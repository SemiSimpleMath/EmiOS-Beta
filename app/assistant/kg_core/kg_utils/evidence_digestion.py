"""Evidence digestion at threshold (fragility review #6, move 2).

kg_node_evidence grows monotonically — every re-observation appends,
nothing consolidates — and it hits the MOST important nodes hardest:
every prompt consumer slices, and for hub nodes the slice IS the blind
spot. Same move as room chat compaction: when a node's live evidence
crosses a threshold, an LLM folds the old tail into ONE consolidated
digest row; prompts read digest + recent rows.

NOTHING IS DELETED. Evidence is provenance: folded originals get
digested_at stamped (kept for audits, window/provenance joins still see
them); the digest row is a normal kg_node_evidence row with
merge_action='digest' whose message_timestamp sits at the folded span's
boundary, so it sorts chronologically before all newer live rows.
Re-digestion folds the previous digest plus the next tail into a new
digest — exactly one live digest row per node at any time.

Spec: scratch/EVIDENCE_HUB_BUDGETS_SPEC.md.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# A node digests when its live (undigested) evidence rows reach this count.
DIGEST_THRESHOLD = 40
# The newest rows stay live verbatim — recent evidence is what judges
# usually need at full granularity.
KEEP_RECENT = 15
# Upper bound on rows folded per node per run: a 700-row tail digests over
# several nights rather than feeding one giant prompt.
MAX_FOLD_PER_NODE = 80
MAX_DIGEST_CHARS = 2000

AGENT_NAME = "kg_maintenance::evidence_digester"


def _row_ts(ev):
    return ev.message_timestamp or ev.created_at


def run_evidence_digestion(*, max_nodes_per_run: int = 5) -> Dict[str, Any]:
    """Nightly compaction pass: digest the heaviest-evidenced nodes.

    Candidates = nodes whose live row count >= DIGEST_THRESHOLD, heaviest
    first, capped per run. One LLM call per node. A failed call skips the
    node (no partial writes); the next run retries it naturally.
    """
    from sqlalchemy import func

    from app.assistant.database.kg_chat_projection import KGNodeEvidence
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.models.db_manager import get_db_manager
    from app.assistant.utils.time_utils import utc_now

    digested_nodes = 0
    rows_folded = 0
    failed = 0

    with get_db_manager().read_session() as session:
        candidates = (
            session.query(KGNodeEvidence.node_id, func.count().label("cnt"))
            .filter(KGNodeEvidence.digested_at.is_(None))
            .group_by(KGNodeEvidence.node_id)
            .having(func.count() >= DIGEST_THRESHOLD)
            .order_by(func.count().desc())
            .limit(max(1, max_nodes_per_run))
            .all()
        )

        work: List[Dict[str, Any]] = []
        for node_id, _cnt in candidates:
            node = session.get(Node, node_id)
            if node is None:
                continue  # orphan evidence — the orphan scan's problem
            live = (
                session.query(KGNodeEvidence)
                .filter(KGNodeEvidence.node_id == node_id)
                .filter(KGNodeEvidence.digested_at.is_(None))
                .all()
            )
            live.sort(key=lambda ev: (_row_ts(ev) is not None, _row_ts(ev)))
            tail = live[: -KEEP_RECENT][:MAX_FOLD_PER_NODE]
            if len(tail) < 2:
                continue  # nothing worth folding
            work.append({
                "node_id": node_id,
                "label": node.label,
                "node_type": node.node_type,
                "identity_sentence": node.identity_sentence,
                "tail_ids": [ev.id for ev in tail],
                "rows": [
                    {
                        "date": (
                            _row_ts(ev).date().isoformat()
                            if _row_ts(ev) else None
                        ),
                        "kind": ev.merge_action,
                        "text": (ev.derived_sentence or ev.source_text or "").strip(),
                    }
                    for ev in tail
                ],
                "boundary_ts": max(_row_ts(ev) for ev in tail if _row_ts(ev)),
                "span": (
                    next((_row_ts(ev).date().isoformat() for ev in tail if _row_ts(ev)), "?"),
                    max((_row_ts(ev).date().isoformat() for ev in tail if _row_ts(ev)), default="?"),
                ),
            })

    for item in work:
        try:
            digest = _generate_digest(item)
        except Exception as e:
            logger.error("[evidence_digestion] digester failed for %r: %s",
                         item["label"], e)
            failed += 1
            continue
        if not digest:
            logger.error("[evidence_digestion] digester returned empty digest "
                         "for %r — skipping (no writes)", item["label"])
            failed += 1
            continue

        n_folded = len(item["tail_ids"])
        with get_db_manager().transaction(op="evidence_digestion") as s:
            from app.assistant.database.kg_chat_projection import KGNodeEvidence as EV
            s.add(EV(
                node_id=item["node_id"],
                derived_sentence=digest,
                source_text=(
                    f"digest of {n_folded} evidence rows spanning "
                    f"{item['span'][0]}..{item['span'][1]}"
                ),
                message_timestamp=item["boundary_ts"],
                merge_action="digest",
            ))
            now = utc_now()
            s.query(EV).filter(EV.id.in_(item["tail_ids"])).update(
                {"digested_at": now}, synchronize_session=False,
            )
        digested_nodes += 1
        rows_folded += n_folded
        logger.info("[evidence_digestion] %r: folded %d rows (%s..%s) into one digest",
                    item["label"], n_folded, item["span"][0], item["span"][1])

    return {
        "candidates": len(work),
        "digested_nodes": digested_nodes,
        "rows_folded": rows_folded,
        "failed": failed,
    }


def _generate_digest(item: Dict[str, Any]) -> str:
    """One LLM call: fold the tail rows into a consolidated chronicle."""
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent

    result = _run_subconscious_agent(
        handler_label="evidence_digester",
        agent_name=AGENT_NAME,
        context={
            "label": item["label"],
            "node_type": item["node_type"],
            "identity_sentence": item["identity_sentence"],
            "rows": item["rows"],
        },
        scope_id=AGENT_NAME,
        actor_id="evidence_digestion",
    )
    if not isinstance(result, dict):
        return ""
    return str(result.get("digest") or "").strip()[:MAX_DIGEST_CHARS]
