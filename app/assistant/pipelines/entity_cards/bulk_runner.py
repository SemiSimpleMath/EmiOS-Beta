"""Bulk runner for the v2 entity-card pipeline.

Reuses v1's deterministic candidate selection (degree/influence/pagerank/
label heuristics + LLM critic gate), then calls v2's
``generate_and_persist_card`` for each surviving candidate. Replaces v1's
``run_entity_card_pipeline`` as the production batch path.

Selection helpers are imported from the v1 module — they're deterministic
SQL/scoring code that doesn't depend on v1's writer. The v1 writer
(``_process_node_worker``) is no longer called by any production code path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from app.assistant.kg_core.user_identity import PRIMARY_USER_NODE_LABEL
from app.assistant.pipelines.entity_cards.kg_entity_card_pipeline.kg_entity_card_pipeline import (
    DEFAULT_MIN_PAGERANK,
    _compute_degree,
    _compute_influence,
    _is_obviously_low_value_label,
    _iter_nodes,
    _load_all_edge_pairs,
    _run_critic_gate,
)
from app.assistant.pipelines.entity_cards.kg_entity_card_pipeline.kg_entity_card_pipeline import (
    get_nodes_updated_since,
    log_maintenance_run,
    get_last_maintenance_run_time,
)
from app.assistant.pipelines.entity_cards.card_writer import generate_and_persist_card
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def run_bulk_entity_cards(
    *,
    incremental: bool = False,
    min_outgoing_edges: int = 1,
    only_missing_cards: Optional[bool] = None,
    allowed_node_types: Optional[Set[str]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Bulk runner: select candidates, then call ``generate_and_persist_card``
    for each. Returns the audit shape consumed by ``GenerateEntityCardsStep``.
    """
    session = get_session()
    if allowed_node_types is None:
        allowed_node_types = {"Entity"}
    if only_missing_cards is None:
        only_missing_cards = False if incremental else True

    run_start_time = datetime.now(timezone.utc)
    candidates: List[Tuple[str, str]] = []
    skipped_count = 0
    total_nodes_estimate = 0

    try:
        edge_pairs = _load_all_edge_pairs(session)
        degree_map = _compute_degree(edge_pairs)
        influence_map = _compute_influence(edge_pairs, degree_map)
        logger.info(
            "[v2] Influence pre-compute: %d edges, %d unique nodes scored",
            len(edge_pairs), len(influence_map),
        )

        incremental_nodes = None
        if incremental:
            last_run_time = get_last_maintenance_run_time(session, "entity_card_generation")
            if not last_run_time:
                last_run_time = datetime.now(timezone.utc) - timedelta(weeks=5)
            incremental_nodes = get_nodes_updated_since(session, last_run_time) or []
            logger.info("[v2] Incremental: %d nodes since %s", len(incremental_nodes), last_run_time)
        else:
            logger.info("[v2] Full processing: min_edges=%d, only_missing=%s",
                        min_outgoing_edges, only_missing_cards)

        node_iter, total_nodes_estimate = _iter_nodes(
            session=session,
            incremental_nodes=incremental_nodes,
            min_outgoing_edges=min_outgoing_edges,
            only_missing_cards=bool(only_missing_cards),
        )

        for node in node_iter:
            if getattr(node, "label", None) == PRIMARY_USER_NODE_LABEL:
                skipped_count += 1
                continue
            node_type = getattr(node, "node_type", None)
            if node_type and node_type not in allowed_node_types:
                skipped_count += 1
                continue
            label = getattr(node, "label", None) or ""
            if _is_obviously_low_value_label(label):
                skipped_count += 1
                continue

            pr = float(getattr(node, "pagerank_score", 0) or 0)
            category = (getattr(node, "category", "") or "").strip().lower()
            is_person = "person" in category.split()
            if not is_person and pr < DEFAULT_MIN_PAGERANK:
                skipped_count += 1
                continue
            candidates.append((str(node.id), label))
    finally:
        session.close()

    logger.info("[v2] Phase 1 done: %d candidates, %d skipped (of %d total)",
                len(candidates), skipped_count, total_nodes_estimate)

    if candidates:
        pre_critic = len(candidates)
        candidates, critic_rejected = _run_critic_gate(candidates)
        logger.info("[v2] Critic gate: %d → %d candidates (%d rejected)",
                    pre_critic, len(candidates), critic_rejected)

    processed_count = 0
    error_count = 0
    agent_rejected_count = 0

    logger.info("[v2] Phase 2: processing %d candidates sequentially", len(candidates))
    for i, (nid, lbl) in enumerate(candidates):
        try:
            generate_and_persist_card(lbl, force=force)
            processed_count += 1
        except Exception as exc:
            logger.error("[v2] Worker crashed for %s: %s", lbl, exc)
            logger.debug("[v2] worker exception detail", exc_info=True)
            error_count += 1
            continue

        done = processed_count + error_count + agent_rejected_count
        if done % 10 == 0 or done == len(candidates):
            logger.info("[v2] Progress: %d/%d (%d created, %d errors)",
                        done, len(candidates), processed_count, error_count)

    run_duration = (datetime.now(timezone.utc) - run_start_time).total_seconds()

    log_session = get_session()
    try:
        log_maintenance_run(
            session=log_session,
            task_name="entity_card_generation",
            last_run_time=datetime.now(timezone.utc),
            nodes_processed=total_nodes_estimate,
            nodes_updated=processed_count,
            run_duration_seconds=run_duration,
        )
    except Exception:
        logger.error("[v2] Failed to log maintenance run")
        logger.debug("[v2] log maintenance exception", exc_info=True)
    finally:
        try:
            log_session.close()
        except Exception:
            pass

    logger.info("[v2] Pipeline completed: %d processed, %d errors, %d skipped, %d rejected",
                processed_count, error_count, skipped_count, agent_rejected_count)
    logger.info("[v2] Run duration: %.2f seconds", run_duration)

    return {
        "processed": processed_count,
        "errors": error_count,
        "skipped": skipped_count,
        "agent_rejected": agent_rejected_count,
        "run_duration_seconds": run_duration,
        "incremental": incremental,
        "only_missing_cards": bool(only_missing_cards),
        "nodes_considered": total_nodes_estimate,
    }
