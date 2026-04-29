"""Read/write API for the pod_store table.

Minimal operations needed right now:
- ``ensure_tables()`` at startup to create the table if missing.
- ``put(pod)`` insert/upsert by pod_id.
- ``get(pod_id)`` fetch one.
- ``query(tags, for_agent, since_utc, limit)`` find pods for a consumer.

Scope enforcement and retention policies can grow here later; kept
narrow for now so PodClassifier can begin minting.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Union

from sqlalchemy import and_, or_, text

from app.assistant.pod_store.contracts import Pod, PodSourceRef
from app.assistant.pod_store.models import PodRow
from app.assistant.utils.logging_config import get_logger
from app.models.base import Base, get_session

logger = get_logger(__name__)


_SINCE_SHORTHAND_RE = re.compile(r"^\s*(\d+)\s*([hdwm])\s*$", re.IGNORECASE)


def _parse_since(value: Union[datetime, str]) -> datetime:
    """Accept a datetime, an ISO string, or a shorthand like '24h', '3d',
    '2w', '1m', or 'today'. Returns a UTC-aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value or "").strip()
    if not s:
        raise ValueError("empty `since` value")
    lowered = s.lower()
    now = datetime.now(timezone.utc)
    if lowered == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = _SINCE_SHORTHAND_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = {
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
            "m": timedelta(days=30 * n),  # rough month — good enough for queries
        }[unit]
        return now - delta
    # Fall back to ISO parsing.
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class PodStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        session = get_session()
        try:
            engine = session.bind
            Base.metadata.create_all(engine, tables=[PodRow.__table__], checkfirst=True)
        finally:
            session.close()

    def put(self, pod: Pod) -> None:
        """Insert a new pod or update an existing one (idempotent by pod_id).

        Also writes (or refreshes) a thin ``kg_node_metadata`` mirror row so
        edges from Entity / Event / State nodes can target this pod. See
        ``kg_mirror.ensure_pod_node`` for the projection rules and the
        edge-direction convention (KG-node → Pod).
        """
        with self._lock:
            session = get_session()
            try:
                row = session.query(PodRow).filter_by(pod_id=pod.pod_id).one_or_none()
                if row is None:
                    row = PodRow(pod_id=pod.pod_id)
                row.kind = pod.kind
                row.tags_json = list(pod.tags)
                row.one_liner = pod.one_liner
                row.body = pod.body
                row.source_refs_json = [sr.model_dump() for sr in pod.source_refs]
                row.for_agents_json = list(pod.for_agents)
                row.scope_id = pod.scope_id
                row.created_by = pod.created_by
                row.metadata_json = dict(pod.metadata) if pod.metadata else None
                session.add(row)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        # Mirror outside the pod_store session — kg_mirror opens its own
        # session against kg_node_metadata. Best-effort: a mirror failure
        # logs but doesn't roll back the pod write (the pod itself is
        # still useful even without the KG projection).
        try:
            from app.assistant.pod_store.kg_mirror import ensure_pod_node
            ensure_pod_node(pod)
        except Exception as e:
            from app.assistant.utils.logging_config import get_logger
            get_logger(__name__).warning(
                "PodStore.put: kg_mirror.ensure_pod_node failed for %s: %s",
                pod.pod_id, e,
            )

    def get(self, pod_id: str) -> Optional[Pod]:
        with self._lock:
            session = get_session()
            try:
                row = session.query(PodRow).filter_by(pod_id=pod_id).one_or_none()
                if row is None:
                    return None
                return self._row_to_pod(row)
            finally:
                session.close()

    def query(
        self,
        *,
        for_agent: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        scope: Optional[str] = None,
        kind: Optional[str] = None,
        linked_to_entity: Optional[str] = None,
        linked_via: Optional[Sequence[str]] = None,
        since: Optional[Union[datetime, str]] = None,
        since_utc: Optional[datetime] = None,
        query: Optional[str] = None,
        limit: int = 50,
    ) -> List[Pod]:
        """Fetch pods a consumer cares about.

        - ``for_agent``: only pods whose ``for_agents`` contains this name.
          SQLite JSON membership is expressed via LIKE-against-serialized
          JSON (good enough at small scale; revisit for >10k pods).
        - ``tags``: restrict to pods whose tags include any of these.
        - ``scope``: only pods whose ``scope_id`` exactly matches (room_id).
        - ``kind``: only pods of this kind (e.g. ``"email"``,
          ``"chat_cluster"``). Useful to narrow a free-text query to one
          source.
        - ``linked_to_entity``: only pods that are the target of a KG edge
          from an Entity node with this label (case-sensitive match).
          E.g., ``linked_to_entity="Jukka"`` returns pods Jukka has any
          outgoing edge to (depicted_in, has_profile_image, has_video, …).
          Combine with ``linked_via`` to narrow by edge type.
        - ``linked_via``: only meaningful with ``linked_to_entity``. List of
          edge relationship_types to accept (e.g.
          ``["depicted_in", "has_profile_image"]``). When None, any edge
          from the entity counts.
        - ``since``: only pods created after this. Accepts a datetime OR a
          shorthand string: ``"24h"``, ``"3d"``, ``"2w"``, ``"today"``, or
          an ISO timestamp. ``since_utc`` kept for backward compatibility.
        - ``query``: substring match over ``one_liner`` and ``body`` (case
          insensitive). When provided, ranking promotes one_liner hits ahead
          of body-only hits, then orders by recency within each group.
        - ``limit``: max rows returned; ordered newest-first (or by query
          relevance when ``query`` is provided).
        """
        resolved_since = since_utc
        if resolved_since is None and since is not None:
            resolved_since = _parse_since(since)

        needle = str(query or "").strip()
        max_rows = max(1, int(limit or 50))

        with self._lock:
            session = get_session()
            try:
                q = session.query(PodRow)
                if resolved_since is not None:
                    q = q.filter(PodRow.created_at >= resolved_since)
                if scope:
                    q = q.filter(PodRow.scope_id == scope)
                if kind:
                    q = q.filter(PodRow.kind == kind)
                if linked_to_entity:
                    # Sub-select pod_ids that are the target of a KG edge
                    # from an Entity node matching the label. Both pod_store
                    # and kg_*_metadata live in emi.db so a cross-table
                    # subquery on the same engine is fine.
                    from app.assistant.kg.db.knowledge_graph_db import Edge, Node
                    pod_id_subq = (
                        session.query(Edge.target_id)
                        .join(Node, Node.id == Edge.source_id)
                        .filter(Node.label == linked_to_entity)
                        .filter(Node.node_type == "Entity")
                    )
                    if linked_via:
                        pod_id_subq = pod_id_subq.filter(
                            Edge.relationship_type.in_(list(linked_via))
                        )
                    q = q.filter(PodRow.pod_id.in_(pod_id_subq))
                if for_agent:
                    # LIKE on the JSON-serialized list. SQLite stores JSON
                    # arrays as text like `["meal_planner","health_watcher"]`.
                    agent_needle = json.dumps(for_agent)
                    q = q.filter(PodRow.for_agents_json.like(f"%{agent_needle}%"))
                if tags:
                    tag_filters = [PodRow.tags_json.like(f"%{json.dumps(t)}%") for t in tags]
                    q = q.filter(or_(*tag_filters))
                if needle:
                    # Tokenize the query; multi-word searches match pods that
                    # contain ANY of the tokens (OR), then rank by how many
                    # tokens hit and whether they hit the one_liner. Never
                    # return zero when some tokens match something — prefer
                    # the best partial match to silence.
                    #
                    # Tokens ending in 's' with length > 3 are also matched
                    # in their singular form (trailing-s stem).
                    tokens = [t for t in re.split(r"[\s,]+", needle) if t]
                    token_stems: list[tuple[str, set[str]]] = []
                    all_filters = []
                    for tok in tokens:
                        stems = {tok}
                        if len(tok) > 3 and tok.lower().endswith("s"):
                            stems.add(tok[:-1])
                        token_stems.append((tok, stems))
                        for stem in stems:
                            like_pattern = f"%{stem}%"
                            all_filters.append(PodRow.one_liner.ilike(like_pattern))
                            all_filters.append(PodRow.body.ilike(like_pattern))
                    q = q.filter(or_(*all_filters))
                    # Wider pool so Python-side ranking has room to promote
                    # pods hitting more tokens above pods hitting fewer.
                    q = q.order_by(PodRow.created_at.desc()).limit(max_rows * 5)
                    rows = q.all()

                    def _score(row: PodRow) -> tuple:
                        # one_liner is curated/dense, body is long/noisy.
                        # Rank by one_liner hits first — body matches are
                        # only used as a tiebreaker and as a last-resort
                        # signal when no pod has any one_liner hits.
                        ol = (row.one_liner or "").lower()
                        body = (row.body or "").lower()
                        tokens_matched = 0
                        one_liner_matched = 0
                        for _tok, stems in token_stems:
                            stems_lower = [s.lower() for s in stems]
                            in_ol = any(s in ol for s in stems_lower)
                            in_body = any(s in body for s in stems_lower)
                            if in_ol or in_body:
                                tokens_matched += 1
                                if in_ol:
                                    one_liner_matched += 1
                        ts = row.created_at.timestamp() if row.created_at else 0
                        return (-one_liner_matched, -tokens_matched, -ts)

                    rows.sort(key=_score)
                    rows = rows[:max_rows]
                else:
                    q = q.order_by(PodRow.created_at.desc()).limit(max_rows)
                    rows = q.all()
                return [self._row_to_pod(r) for r in rows]
            finally:
                session.close()

    @staticmethod
    def _row_to_pod(row: PodRow) -> Pod:
        raw_refs = row.source_refs_json or []
        refs: List[PodSourceRef] = []
        for item in raw_refs:
            if isinstance(item, dict):
                try:
                    refs.append(PodSourceRef(**item))
                except Exception as e:
                    logger.error("PodStore: skipping malformed source_ref on pod_id=%s: %s", row.pod_id, e)
        return Pod(
            pod_id=row.pod_id,
            kind=row.kind,
            tags=list(row.tags_json or []),
            one_liner=row.one_liner or "",
            body=row.body,
            source_refs=refs,
            for_agents=list(row.for_agents_json or []),
            scope_id=row.scope_id,
            created_by=row.created_by,
            created_at=row.created_at,
            metadata=dict(row.metadata_json or {}),
        )
