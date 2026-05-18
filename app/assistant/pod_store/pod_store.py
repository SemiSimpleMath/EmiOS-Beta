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
            from app.assistant.pod_store.models import PodProjection, PodAudit
            Base.metadata.create_all(
                engine,
                tables=[
                    PodRow.__table__,
                    PodProjection.__table__,
                    PodAudit.__table__,
                ],
                checkfirst=True,
            )
        finally:
            session.close()

    def put(self, pod: Pod) -> None:
        """Insert a new pod or update an existing one (idempotent by pod_id).

        pod_store is the sole source of truth for pod content. The KG
        references pods by URI directly (no kg_node mirror); the promoter
        checks `pod_kind_registry.is_kg_admissible()` when writing edges
        with `datapod:*` endpoints.
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

    # ── Authority-gated projection API ────────────────────────────────────
    #
    # For identity and artifact pods that carry multiple projections at
    # different authority levels. See app/assistant/pod_store/authority.py
    # for the band definitions and authority.check_authority for the gate.

    def fetch_projection(self, pod_id: str, projection: str, *, scope) -> str:
        """Read one projection of a pod after authority check.

        Returns the projection value (string for plain/env; raw bytes
        for file are returned as bytes — caller should know the
        projection's storage_kind via list_projections).

        Raises:
            PodAuthorityError if scope.authority < projection.min_authority
            PodValueMissing if the underlying storage (env var, file)
              is absent
            KeyError if the pod or projection doesn't exist
        """
        from app.assistant.pod_store.models import PodProjection
        from app.assistant.pod_store.authority import check_authority
        from app.assistant.pod_store.resolvers import (
            resolve_plain, resolve_env, resolve_file,
        )
        session = get_session()
        try:
            row = (
                session.query(PodProjection)
                .filter(
                    PodProjection.pod_id == pod_id,
                    PodProjection.projection_name == projection,
                )
                .first()
            )
            if row is None:
                self._audit(
                    session, pod_id=pod_id, projection=projection,
                    operation="fetch", scope=scope,
                    outcome="denied", detail="projection not found",
                )
                raise KeyError(
                    f"pod {pod_id} has no projection {projection!r}"
                )
            actual = check_authority(
                pod_id=pod_id,
                projection=projection,
                required=row.min_authority,
                scope=scope,
            )
            # Audit BEFORE resolving — covers the case where resolve_*
            # raises PodValueMissing (pointer is stale). We logged that
            # the read was authorized; the failure was infrastructure.
            self._audit(
                session, pod_id=pod_id, projection=projection,
                operation="fetch", scope=scope,
                outcome="allowed",
                detail=f"storage={row.storage_kind} env_ref={row.env_ref or ''}",
            )
            session.commit()
            if row.storage_kind == "plain":
                return resolve_plain(row.plain_value)
            if row.storage_kind == "env":
                return resolve_env(row.env_ref)
            if row.storage_kind == "file":
                return resolve_file(row.file_ref)
            raise ValueError(
                f"pod {pod_id} projection {projection!r} has unknown "
                f"storage_kind={row.storage_kind!r}"
            )
        finally:
            session.close()

    def list_projections(self, pod_id: str, *, scope) -> List[Dict[str, Any]]:
        """List projection names a caller can fetch for this pod.

        Filters by authority: projections whose min_authority exceeds
        the caller's authority are NOT included in the result.  An
        agent at AUTH_CHAT(50) listing an SSN pod sees `last4`,
        `redacted`, `format` and not `full`, `area_code` — so it can't
        even know `full` exists by enumeration.

        Returns a list of dicts with keys: projection_name,
        min_authority, storage_kind.
        """
        from app.assistant.pod_store.models import PodProjection
        from app.assistant.pod_store.authority import caller_authority
        session = get_session()
        try:
            actual = caller_authority(scope)
            rows = (
                session.query(PodProjection)
                .filter(
                    PodProjection.pod_id == pod_id,
                    PodProjection.min_authority <= actual,
                )
                .order_by(PodProjection.min_authority.asc())
                .all()
            )
            self._audit(
                session, pod_id=pod_id, projection=None,
                operation="list", scope=scope,
                outcome="allowed",
                detail=f"visible={len(rows)}",
            )
            session.commit()
            return [
                {
                    "projection_name": r.projection_name,
                    "min_authority": r.min_authority,
                    "storage_kind": r.storage_kind,
                }
                for r in rows
            ]
        finally:
            session.close()

    def put_secret_pod(
        self,
        *,
        pod_type: str,
        owner_subject_id: str,
        name: str,
        env_ref: str,
        scope,
    ) -> str:
        """Create an identity-style pod with materialized projections.

        Reads the raw value once from `os.environ[env_ref]`, hands it
        to the materializer for `pod_type`, writes the pod_store row
        and all pod_projection rows in a single transaction. Returns
        the new pod_id.

        Authority: requires AUTH_USER (99) — only the user-equivalent
        master_room surface can create new identity pods. This is
        deliberate; minting a new secret pod is a sensitive operation
        in itself.

        Raises:
            PodAuthorityError if scope.authority < AUTH_USER
            PodValueMissing if env_ref is unset
            KeyError if no materializer is registered for pod_type
            ValueError if the materializer rejects the raw value
        """
        import os, uuid
        from app.assistant.pod_store.models import PodProjection
        from app.assistant.pod_store.authority import (
            check_authority, AUTH_USER,
        )
        from app.assistant.pod_store.resolvers import PodValueMissing
        from app.assistant.pod_store.materializers import get_materializer

        pod_id = f"datapod:{pod_type}:{uuid.uuid4().hex[:16]}"
        session = get_session()
        try:
            check_authority(
                pod_id=pod_id, projection=None,
                required=AUTH_USER, scope=scope,
            )
            raw_value = os.environ.get(env_ref)
            if not raw_value:
                raise PodValueMissing(
                    f"env var {env_ref!r} is unset or empty — add it "
                    f"to .env and restart Flask, then retry"
                )
            materialize = get_materializer(pod_type)
            specs = materialize(raw_value=raw_value, env_ref=env_ref)
            # raw_value goes out of scope here; only the env_ref pointer
            # persists in the database for the full projection.
            del raw_value

            # Determine pod-level min_authority as the lowest projection
            # band (any agent who can read any projection should at
            # least be able to query the pod's existence).
            pod_min_authority = min(s.min_authority for s in specs)

            row = PodRow(
                pod_id=pod_id,
                kind=pod_type,
                tags_json=[],
                one_liner=name,
                body=None,
                source_refs_json=[],
                for_agents_json=[],
                scope_id=None,
                min_authority=pod_min_authority,
                created_by=getattr(scope, "actor_id", None),
                metadata_json={
                    "owner_subject_id": owner_subject_id,
                    "env_ref_root": env_ref,
                },
            )
            session.add(row)
            session.flush()

            for spec in specs:
                session.add(PodProjection(
                    id=str(uuid.uuid4()),
                    pod_id=pod_id,
                    projection_name=spec.projection_name,
                    min_authority=spec.min_authority,
                    storage_kind=spec.storage_kind,
                    plain_value=spec.plain_value,
                    env_ref=spec.env_ref,
                    file_ref=spec.file_ref,
                ))
            self._audit(
                session, pod_id=pod_id, projection=None,
                operation="put", scope=scope,
                outcome="allowed",
                detail=f"type={pod_type} projections={len(specs)}",
            )
            session.commit()
            return pod_id
        finally:
            session.close()

    def _audit(
        self,
        session,
        *,
        pod_id: str,
        projection,
        operation: str,
        scope,
        outcome: str,
        detail: str,
    ) -> None:
        """Internal: write one PodAudit row. Best-effort — audit
        failures should never block the underlying operation, but
        they're rare enough that letting them propagate would catch
        infrastructure breakage early. Caller is responsible for
        commit/rollback of the surrounding transaction.
        """
        import uuid
        from app.assistant.pod_store.models import PodAudit
        from app.assistant.pod_store.authority import caller_authority
        session.add(PodAudit(
            id=str(uuid.uuid4()),
            pod_id=pod_id,
            projection_name=projection,
            operation=operation,
            caller_scope_id=getattr(scope, "scope_id", None),
            caller_scope_authority=caller_authority(scope),
            caller_agent=getattr(scope, "actor_id", None),
            caller_request_id=None,
            outcome=outcome,
            detail=detail,
        ))

    # ── End of projection API ─────────────────────────────────────────────

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
