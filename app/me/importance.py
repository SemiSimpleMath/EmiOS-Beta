"""LLM-rated node importance for the lens.

Hits the cheap nano model in batches over all Entity nodes, stores the
result in a JSON file. The lens uses these scores instead of (or
alongside) personalized PageRank to decide which nodes to admit at the
top-K cut and which to surface at far zoom.

Why this exists: PageRank ranks by graph centrality, which doesn't always
match the user's intuition. Mewgenics had high PR (lots of mentions),
Katy had low PR (few KG edges) — but Katy is obviously more important.
A small LLM with good calibration anchors gives a much closer signal at
trivial cost (~50 calls × ~5000 tokens each = a few cents per refresh).

Usage:
    python -m app.me.importance         # rate everything, persist to JSON
    from app.me.importance import get_importance
    score = get_importance(node_id)     # 0-10 float; 5.0 default if unrated
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import or_

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

IMPORTANCE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "me_node_importance.json"
)

DEFAULT_SCORE = 5.0
BATCH_SIZE = 10  # nodes per LLM call. Smaller after we started feeding edge
                 # context — the bigger per-node prompt means we keep the
                 # per-call payload manageable for the small rater model.
EDGES_PER_NODE = 8  # cap on edges shown per node, sorted by edge importance desc.

_CACHE: Optional[Dict[str, float]] = None


def get_importance_map() -> Dict[str, float]:
    """Lazy-loaded importance map keyed by node id."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if IMPORTANCE_PATH.exists():
        try:
            data = json.loads(IMPORTANCE_PATH.read_text(encoding="utf-8"))
            _CACHE = {str(k): float(v) for k, v in data.items()}
            logger.info("me importance: loaded %d scores from %s", len(_CACHE), IMPORTANCE_PATH)
            return _CACHE
        except Exception as e:
            logger.warning("me importance: failed to load %s: %s", IMPORTANCE_PATH, e)
    _CACHE = {}
    return _CACHE


def get_importance(node_id: str) -> float:
    return get_importance_map().get(node_id, DEFAULT_SCORE)


def invalidate() -> None:
    global _CACHE
    _CACHE = None


def _build_node_block(node: Node, edges: Optional[List[Dict[str, Any]]] = None) -> str:
    """Compact text representation of one node for the rater prompt.
    Includes top-N edges (sorted by edge importance) so the rater can see
    the node's graph context, not just label + description."""
    parts: List[str] = [f"id={node.id}"]
    parts.append(f"label={node.label!r}")
    parts.append(f"type={node.node_type or '-'}")
    if node.category:
        parts.append(f"category={node.category!r}")
    desc = (node.description or "").strip()
    if desc:
        if len(desc) > 240:
            desc = desc[:237] + "..."
        parts.append(f"desc={desc!r}")
    aliases = node.aliases if isinstance(node.aliases, list) else []
    if aliases:
        parts.append(f"aliases={[str(a) for a in aliases[:5]]!r}")
    head = " | ".join(parts)
    if not edges:
        return head
    edge_lines = [f"  {e['direction']} {e['predicate']}: {e['other_label']}" for e in edges]
    return head + "\n" + "\n".join(edge_lines)


def _fetch_node_edges_batch(
    session, node_ids: List[str], cap: int = EDGES_PER_NODE
) -> Dict[str, List[Dict[str, Any]]]:
    """For each node_id in `node_ids`, return up to `cap` edges sorted by
    importance desc. Each edge dict: predicate, other_label, direction, importance.
    One DB roundtrip for edges + one for missing labels."""
    if not node_ids:
        return {}
    node_id_set = {str(nid) for nid in node_ids}
    edges = session.query(Edge).filter(
        or_(Edge.source_id.in_(node_ids), Edge.target_id.in_(node_ids))
    ).all()
    # Collect labels we need (other side of each edge, plus our own batch nodes
    # in case a self-loop or batch-internal edge appears).
    referenced_ids: set[str] = set()
    for e in edges:
        referenced_ids.add(str(e.source_id))
        referenced_ids.add(str(e.target_id))
    label_map: Dict[str, str] = {}
    if referenced_ids:
        for nid, label in session.query(Node.id, Node.label).filter(
            Node.id.in_(list(referenced_ids))
        ).all():
            label_map[str(nid)] = label or ""
    by_node: Dict[str, List[Dict[str, Any]]] = {nid: [] for nid in node_id_set}
    for e in edges:
        sid = str(e.source_id)
        tid = str(e.target_id)
        imp = float(e.importance) if e.importance is not None else 0.0
        predicate = (e.relationship_type or "").strip() or "?"
        if sid in node_id_set:
            by_node[sid].append({
                "predicate": predicate,
                "other_label": label_map.get(tid, ""),
                "direction": "→",
                "importance": imp,
            })
        if tid in node_id_set and tid != sid:
            by_node[tid].append({
                "predicate": predicate,
                "other_label": label_map.get(sid, ""),
                "direction": "←",
                "importance": imp,
            })
    for nid in by_node:
        by_node[nid].sort(key=lambda r: -r["importance"])
        by_node[nid] = by_node[nid][:cap]
    return by_node


def regenerate_importance(
    *,
    batch_size: int = BATCH_SIZE,
    only_node_types: Optional[List[str]] = None,
    only_unrated: bool = False,
) -> Dict[str, float]:
    """Rate Entity nodes and persist to JSON + kg_node_metadata.importance.

    Args:
        batch_size: nodes per LLM call.
        only_node_types: defaults to ["Entity"]. Pass ["Entity", "State"]
            to also rate states (rarely useful — the lens treats states as
            connective tissue, not standalone-rankable).
        only_unrated: when True, skip nodes whose ``kg_node_metadata.importance``
            is non-null. Used by the periodic backfill routine to rate only
            newly-promoted content without re-rating the whole graph each
            tick. Default False = re-rate everything (full-refresh mode).
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import (
        Message,
        ScopeApprovalPolicy,
        ScopeContext,
        ScopeResourcePolicy,
    )

    types_filter = only_node_types or ["Entity"]
    started = time.time()

    with get_db_manager().read_session() as session:
        query = session.query(Node).filter(Node.node_type.in_(types_filter))
        if only_unrated:
            query = query.filter(Node.importance.is_(None))
        all_nodes = query.all()

    logger.info(
        "me importance: rating %d nodes in batches of %d",
        len(all_nodes), batch_size,
    )

    scope = ScopeContext(
        scope_id="scope::me::importance",
        owner_id="jukka",
        actor_id="me_importance_runner",
        surface="ui",
        room_id="me_lens",
        approval=ScopeApprovalPolicy(authority_level=99),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )

    scores: Dict[str, float] = {}
    failures = 0

    for i in range(0, len(all_nodes), batch_size):
        batch = all_nodes[i : i + batch_size]
        batch_ids = [str(n.id) for n in batch]
        # Pre-fetch edges for this batch in one short read session, before the
        # LLM call. Keeps DB sessions out of LLM-call lifetimes.
        with get_db_manager().read_session() as session:
            edges_by_node = _fetch_node_edges_batch(session, batch_ids, cap=EDGES_PER_NODE)
        batch_text = "\n\n".join(
            _build_node_block(n, edges_by_node.get(str(n.id), [])) for n in batch
        )
        agent = DI.agent_factory.create_agent("me::importance_rater")
        if agent is None:
            logger.error("me importance: agent unavailable")
            break

        msg = Message(
            agent_input={
                "task": batch_text,
                "information": "",
            },
            task=batch_text,
            information="",
            scope_context=scope,
        )
        try:
            result = agent.action_handler(msg)
            data = getattr(result, "data", None) or {}
            ratings = data.get("ratings") or []
            id_set = {str(n.id) for n in batch}
            for r in ratings:
                if not isinstance(r, dict):
                    continue
                nid = str(r.get("id") or "").strip()
                if nid not in id_set:
                    continue
                try:
                    score = float(r.get("score") or DEFAULT_SCORE)
                except (TypeError, ValueError):
                    continue
                scores[nid] = max(0.0, min(10.0, score))
            logger.info(
                "me importance: batch %d/%d → %d ratings (running total %d)",
                (i // batch_size) + 1,
                (len(all_nodes) + batch_size - 1) // batch_size,
                len([r for r in ratings if isinstance(r, dict)]),
                len(scores),
            )
        except Exception as e:
            failures += 1
            logger.error("me importance: batch %d failed: %s", (i // batch_size) + 1, e)
            if failures >= 5:
                logger.error("me importance: too many failures, aborting")
                break

    # Default-fill anything we didn't rate so the lens doesn't see Nones.
    # In only_unrated mode we DON'T default-fill — we only persist real scores
    # for nodes we actually rated this run (otherwise we'd write 5.0 to every
    # unrated node and immediately lose the "is unrated?" signal).
    if not only_unrated:
        for n in all_nodes:
            scores.setdefault(str(n.id), DEFAULT_SCORE)

    # Persist to JSON (lens cache).
    IMPORTANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if only_unrated:
        # Merge into the existing JSON map so we don't lose prior scores.
        existing = {}
        if IMPORTANCE_PATH.exists():
            try:
                existing = json.loads(IMPORTANCE_PATH.read_text(encoding="utf-8"))
                existing = {str(k): float(v) for k, v in existing.items()}
            except Exception as e:
                logger.warning("me importance: could not read existing JSON for merge: %s", e)
        existing.update(scores)
        IMPORTANCE_PATH.write_text(json.dumps(existing, indent=0), encoding="utf-8")
    else:
        IMPORTANCE_PATH.write_text(json.dumps(scores, indent=0), encoding="utf-8")
    invalidate()  # force reload next get

    # Persist to kg_node_metadata.importance — this is what the wiki refresh's
    # importance pre-filter reads. Without the DB write, the SQL filter would
    # always see NULL and either drop or admit everything depending on policy.
    #
    # CRITICAL: must NOT bump Node.updated_at. Importance is a derived score
    # (recomputed from graph topology each lens run) — a fresh score is not
    # a fresh node. Bumping updated_at here triggers
    # change_detection.find_changed_neighborhood_nodes to mark every wiki +
    # entity card whose neighborhood includes the rated node as "changed",
    # cascading into wiki refreshes and card regenerations on no semantic
    # change at all. The Node.updated_at = Node.updated_at self-reference
    # in .values() suppresses the column's onupdate=func.now() hook so the
    # timestamp is preserved verbatim. Same pattern as persist_description.
    persisted_to_db = 0
    if scores:
        with get_db_manager().transaction(op="me_importance_persist") as session:
            for nid, sc in scores.items():
                try:
                    session.query(Node).filter(Node.id == nid).update(
                        {Node.importance: float(sc), Node.updated_at: Node.updated_at},
                        synchronize_session=False,
                    )
                    persisted_to_db += 1
                except Exception as e:
                    logger.warning("me importance: DB write failed for node %s: %s", nid, e)
            session.commit()

    elapsed = time.time() - started
    logger.info(
        "me importance: rated %d nodes in %.1fs (%d failures); JSON=%s DB_writes=%d",
        len(scores), elapsed, failures, IMPORTANCE_PATH, persisted_to_db,
    )
    return scores


if __name__ == "__main__":  # pragma: no cover
    import app.assistant.tests.test_setup  # noqa: F401
    regenerate_importance()
