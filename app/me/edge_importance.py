"""LLM-rated edge importance for the lens.

Iterates over every KG edge in batches and asks `me::edge_importance_rater`
to score it 0-10 from the source's perspective. Scores are written to the
`kg_edge.importance` column directly — that column was reserved for this
purpose and currently holds NULL for every edge.

Why this exists: PageRank distributes importance through edges weighted by
their importance. Without per-edge importance, all edges have flat weight
and PR can't distinguish "Phil ↔ Phil's dad" (heavy family bond) from
"Jukka ↔ Phil's dad" (met-once acquaintance). With per-edge importance,
relationship strength flows through the graph correctly.

Usage:
    python -m app.me.edge_importance         # rate every edge, persist to DB
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

DEFAULT_SCORE = 5.0
BATCH_SIZE = 50  # ~50 edges per LLM call


def _build_edge_block(
    edge: Edge,
    node_by_id: Dict[str, Node],
) -> Optional[str]:
    """Multi-line text representation of one edge for the rater prompt.

    Each field on its own line so the model reads them cleanly. The
    EDGE SENTENCE is the most important signal — it's the one whose
    importance the rater is being asked to score — so it gets all-caps
    framing to make sure the model anchors on it.

    Returns None if either endpoint is missing — the edge is malformed.
    """
    src = node_by_id.get(str(edge.source_id))
    tgt = node_by_id.get(str(edge.target_id))
    if src is None or tgt is None:
        return None

    def _node_desc(n: Node) -> str:
        desc = (n.description or "").strip()
        if not desc:
            return "-"
        if len(desc) > 240:
            desc = desc[:237] + "..."
        return desc

    edge_sent = (edge.sentence or "").strip()
    if len(edge_sent) > 280:
        edge_sent = edge_sent[:277] + "..."

    lines = [
        f"id: {edge.id}",
        f"source label: {src.label or '-'}",
        f"edge label: {edge.relationship_type or '-'}",
        f"target label: {tgt.label or '-'}",
        f"source sentence: {_node_desc(src)}",
        f"target sentence: {_node_desc(tgt)}",
        f"EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: {edge_sent or '-'}",
    ]
    return "\n".join(lines)


def regenerate_edge_importance(
    *,
    batch_size: int = BATCH_SIZE,
    only_unrated: bool = True,
) -> int:
    """Rate every edge and write to kg_edge.importance.

    Args:
        batch_size: edges per LLM call.
        only_unrated: when True (default), skip edges that already have a
            non-NULL importance value. Set False to fully re-rate.

    Returns: number of edges scored this run.
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import (
        Message,
        ScopeApprovalPolicy,
        ScopeContext,
        ScopeResourcePolicy,
    )

    started = time.time()

    # Load all nodes once for label/category lookup; avoids per-batch joins.
    with get_db_manager().read_session() as session:
        node_by_id: Dict[str, Node] = {
            str(n.id): n for n in session.query(Node).all()
        }
        # Pull all edges to be rated.
        if only_unrated:
            all_edges = session.query(Edge).filter(Edge.importance.is_(None)).all()
        else:
            all_edges = session.query(Edge).all()

    logger.info(
        "me edge importance: rating %d edges in batches of %d (only_unrated=%s)",
        len(all_edges), batch_size, only_unrated,
    )

    scope = ScopeContext(
        scope_id="scope::me::edge_importance",
        owner_id="jukka",
        actor_id="me_edge_importance_runner",
        surface="ui",
        room_id="me_lens",
        approval=ScopeApprovalPolicy(authority_level=99),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )

    rated_count = 0
    failures = 0

    for i in range(0, len(all_edges), batch_size):
        batch = all_edges[i : i + batch_size]
        # Build the prompt block — skip edges with missing endpoints.
        prompt_lines: List[str] = []
        batch_ids: List[str] = []
        for e in batch:
            block = _build_edge_block(e, node_by_id)
            if block is None:
                continue
            prompt_lines.append(block)
            batch_ids.append(str(e.id))
        if not prompt_lines:
            continue
        batch_text = "\n".join(prompt_lines)

        agent = DI.agent_factory.create_agent("me::edge_importance_rater")
        if agent is None:
            logger.error("me edge importance: agent unavailable")
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
            id_set = set(batch_ids)
            scored: Dict[str, float] = {}
            for r in ratings:
                if not isinstance(r, dict):
                    continue
                eid = str(r.get("id") or "").strip()
                if eid not in id_set:
                    continue
                try:
                    score = float(r.get("score") or DEFAULT_SCORE)
                except (TypeError, ValueError):
                    continue
                scored[eid] = max(0.0, min(10.0, score))

            # Default-fill any edge in this batch that didn't get a rating.
            for eid in batch_ids:
                scored.setdefault(eid, DEFAULT_SCORE)

            # Write to DB. One transaction per batch keeps progress durable
            # if the run is interrupted partway.
            with get_db_manager().transaction(op="me_edge_importance") as session:
                _write_batch(session, scored)
            rated_count += len(scored)

            logger.info(
                "me edge importance: batch %d/%d → %d ratings (running total %d)",
                (i // batch_size) + 1,
                (len(all_edges) + batch_size - 1) // batch_size,
                len(scored),
                rated_count,
            )
        except Exception as e:
            failures += 1
            logger.error("me edge importance: batch %d failed: %s", (i // batch_size) + 1, e)
            if failures >= 5:
                logger.error("me edge importance: too many failures, aborting")
                break

    elapsed = time.time() - started
    logger.info(
        "me edge importance: rated %d edges in %.1fs (%d failures)",
        rated_count, elapsed, failures,
    )
    return rated_count


def _write_batch(session: Session, scored: Dict[str, float]) -> None:
    """Update kg_edge.importance for a batch of edges in a single transaction."""
    if not scored:
        return
    # Bulk fetch the rows by id, then assign — avoids N round trips.
    edges = session.query(Edge).filter(Edge.id.in_(list(scored.keys()))).all()
    for e in edges:
        score = scored.get(str(e.id))
        if score is None:
            continue
        e.importance = float(score)


if __name__ == "__main__":  # pragma: no cover
    import app.assistant.tests.test_setup  # noqa: F401
    regenerate_edge_importance()
