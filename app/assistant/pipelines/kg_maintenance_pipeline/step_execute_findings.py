"""
Step: execute_findings

Automatically executes pending maintenance findings that have safe,
well-defined actions:

  - orphan_node  → delete (if node still has zero edges)
  - duplicate_node → merge (reroute edges, merge fields, delete loser)

Suspect-node findings are NOT auto-executed — they require human review
or a redesigned evaluation pipeline.

Each finding is processed in its own transaction.  On failure the finding
is marked ``execute_error`` and the step continues to the next one.
"""
from __future__ import annotations

from typing import Any, Optional

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

AUTO_EXECUTE_TYPES = {"orphan_node", "duplicate_node"}


# ---------------------------------------------------------------------------
# Orphan execution
# ---------------------------------------------------------------------------

def _execute_orphan(finding: dict) -> dict:
    """
    Delete an orphan node if it still has zero edges.

    Returns a result dict with ``executed`` bool and ``detail`` string.
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge, Node
    from app.assistant.kg_core.kg_utils.kg_tools import delete_node

    node_id = finding["primary_node_id"]

    session = get_session()
    try:
        node = session.query(Node).filter_by(id=node_id).first()
        if node is None:
            return {"executed": True, "detail": "Node already deleted."}

        edge_count = (
            session.query(Edge.id)
            .filter((Edge.source_id == node_id) | (Edge.target_id == node_id))
            .count()
        )
        if edge_count > 0:
            return {"executed": False, "detail": f"Node gained {edge_count} edge(s) since scan — skipping."}

        label = node.label or ""
        node_type = node.node_type or ""
        delete_node(node_id, session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _delete_node_embedding(node_id)

    return {
        "executed": True,
        "detail": f"Deleted orphan node '{label}' (type={node_type}).",
    }


# ---------------------------------------------------------------------------
# Duplicate execution
# ---------------------------------------------------------------------------

def _execute_duplicate(finding: dict) -> dict:
    """
    Merge two duplicate nodes: keep the canonical one (winner), reroute the
    loser's edges, merge fields via LLM, delete loser.

    The LLM agent (kg_maintenance::node_data_merger) intelligently combines
    aliases, tags, descriptions, dates, and semantic fields.  The DB session
    is closed before the LLM call and reopened for the write phase.

    Returns a result dict.
    """
    from app.assistant.kg.db.knowledge_graph_db import Node
    from app.assistant.kg_core.kg_utils.node_merge import (
        merge_nodes_in_session,
        snapshot_node,
    )
    from app.assistant.pipelines.kg_shared import (
        apply_node_data_merger_result,
        merge_node_fields_into_existing,
    )

    node_a_id = finding["primary_node_id"]
    node_b_id = finding["secondary_node_id"]

    if not node_b_id:
        return {"executed": False, "detail": "Finding has no secondary_node_id."}

    # Phase 1: read both nodes (short session)
    session = get_session()
    try:
        node_a = session.query(Node).filter_by(id=node_a_id).first()
        node_b = session.query(Node).filter_by(id=node_b_id).first()

        if node_a is None and node_b is None:
            return {"executed": True, "detail": "Both nodes already deleted."}
        if node_a is None:
            return {"executed": True, "detail": f"Node A ({node_a_id[:12]}) already deleted; B remains."}
        if node_b is None:
            return {"executed": True, "detail": f"Node B ({node_b_id[:12]}) already deleted; A remains."}

        winner, loser = _pick_canonical(node_a, node_b, session)
        winner_id = str(winner.id)
        loser_id = str(loser.id)
        winner_label = winner.label
        loser_label = loser.label
        winner_data = _node_to_dict(winner)
        loser_data = _node_to_dict(loser)
    finally:
        session.close()

    # Phase 2: LLM field merge (no session open)
    merged_fields = _llm_merge_fields(winner_data, loser_data)

    # Phase 3: apply merge in a single transaction via the central helper.
    #   - Snapshot the winner BEFORE applying field merges so the merge log
    #     captures the pre-mutation state (needed for unmerge to restore the
    #     winner's original fields).
    #   - merge_nodes_in_session does: reroute edges, rebind dependents via
    #     NODE_ID_REFERENCES, write kg_merge_log, delete loser.
    session = get_session()
    try:
        winner_node = session.query(Node).filter_by(id=winner_id).first()
        loser_node = session.query(Node).filter_by(id=loser_id).first()

        if winner_node is None:
            return {"executed": False, "detail": f"Winner node '{winner_label}' disappeared during LLM call."}
        if loser_node is None:
            return {"executed": True, "detail": f"Loser node '{loser_label}' disappeared during LLM call; winner remains."}

        winner_pre_snapshot = snapshot_node(winner_node)

        if merged_fields:
            apply_node_data_merger_result(winner_node, merged_fields)
            if merged_fields.get("merged_description"):
                winner_node.description = merged_fields["merged_description"]
        else:
            merge_node_fields_into_existing(winner_node, loser_data)
        session.flush()

        merge_log_id = merge_nodes_in_session(
            session,
            loser_node=loser_node,
            winner_node=winner_node,
            merge_actor="kg_maintenance::_execute_duplicate",
            notes=f"merge_method={'LLM' if merged_fields else 'field-copy'}",
            winner_pre_snapshot=winner_pre_snapshot,
        )

        # Pull counts from the log row before we commit so the return detail
        # stays faithful.
        from app.assistant.database.kg_merge_log import KGMergeLog
        log_row = session.query(KGMergeLog).filter_by(id=merge_log_id).first()
        edges_rerouted = len(log_row.rerouted_edge_ids_json or [])
        edges_dropped = len(log_row.dropped_edge_snapshots_json or [])

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    _delete_node_embedding(loser_id)

    merge_method = "LLM" if merged_fields else "field-copy"
    return {
        "executed": True,
        "detail": (
            f"Merged '{loser_label}' into '{winner_label}' ({merge_method}). "
            f"Edges rerouted: {edges_rerouted}, dropped (conflict): {edges_dropped}. "
            f"Merge log id: {merge_log_id[:8]}."
        ),
    }


def _llm_merge_fields(winner_data: dict, loser_data: dict) -> Optional[dict]:
    """
    Call kg_maintenance::node_data_merger to intelligently merge fields.
    Returns the agent's merged field dict, or None on failure (caller falls
    back to simple field-copy merge).
    """
    import json
    from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message

    try:
        agent = DI.agent_factory.create_agent("kg_maintenance::node_data_merger")
        if agent is None:
            logger.error("[execute_findings] Failed to create kg_maintenance::node_data_merger agent")
            return None

        scope_context = build_pipeline_scope_context(
            pipeline_id="kg_maintenance_pipeline",
            actor_id="kg_maintenance_executor",
        )

        response = agent.action_handler(
            Message(
                agent_input={
                    "existing_node_data": json.dumps(winner_data, ensure_ascii=True, default=str),
                    "new_node_data": json.dumps(loser_data, ensure_ascii=True, default=str),
                },
                scope_context=scope_context,
            )
        )

        if response and response.data:
            logger.info(
                "[execute_findings] LLM merge for '%s' ← '%s': confidence=%.2f",
                winner_data.get("label", "?"),
                loser_data.get("label", "?"),
                response.data.get("merge_confidence", 0),
            )
            return response.data

        logger.error("[execute_findings] node_data_merger returned empty result")
        return None

    except Exception as exc:
        logger.error("[execute_findings] LLM merge failed, falling back to field-copy: %s", exc)
        logger.debug("[execute_findings] LLM merge exception", exc_info=True)
        return None


def _pick_canonical(node_a, node_b, session) -> tuple:
    """
    Choose which node to keep (winner) and which to absorb (loser).
    Prefer: higher pagerank_score → more edges → older created_at.
    """
    from app.assistant.kg.db.knowledge_graph_db import Edge

    def _edge_count(node_id: str) -> int:
        return (
            session.query(Edge.id)
            .filter((Edge.source_id == node_id) | (Edge.target_id == node_id))
            .count()
        )

    pr_a = node_a.pagerank_score or 0.0
    pr_b = node_b.pagerank_score or 0.0
    if pr_a != pr_b:
        return (node_a, node_b) if pr_a >= pr_b else (node_b, node_a)

    ec_a = _edge_count(str(node_a.id))
    ec_b = _edge_count(str(node_b.id))
    if ec_a != ec_b:
        return (node_a, node_b) if ec_a >= ec_b else (node_b, node_a)

    if node_a.created_at and node_b.created_at:
        return (node_a, node_b) if node_a.created_at <= node_b.created_at else (node_b, node_a)

    return (node_a, node_b)


def _node_to_dict(node) -> dict[str, Any]:
    """Extract a plain dict from a Node ORM object for merge_node_fields_into_existing."""
    return {
        "label": node.label,
        "node_type": node.node_type,
        "aliases": node.aliases or [],
        "hash_tags": node.hash_tags or [],
        "semantic_label": node.semantic_label,
        "goal_status": node.goal_status,
        "valid_during": node.valid_during,
        "category": node.category,
        "start_date": node.start_date,
        "end_date": node.end_date,
        "start_date_confidence": node.start_date_confidence,
        "end_date_confidence": node.end_date_confidence,
        "confidence": node.confidence,
        "importance": node.importance,
    }


# ---------------------------------------------------------------------------
# ChromaDB cleanup (best-effort)
# ---------------------------------------------------------------------------

def _delete_node_embedding(node_id: str) -> None:
    try:
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        cm = get_chroma_manager()
        cm.delete_node_embedding(node_id)
        cm.delete_node_context_embedding(node_id)
    except Exception as exc:
        logger.debug("ChromaDB embedding cleanup failed for node %s: %s", node_id, exc)


# ---------------------------------------------------------------------------
# Finding-level dispatcher
# ---------------------------------------------------------------------------

_EXECUTORS = {
    "orphan_node": _execute_orphan,
    "duplicate_node": _execute_duplicate,
}


def execute_single_finding(finding: dict) -> dict:
    """
    Execute a single finding.  Returns a result dict with at least
    ``executed`` (bool) and ``detail`` (str).

    Raises on unexpected errors (caller should catch and mark execute_error).
    """
    ftype = finding.get("finding_type", "")
    executor = _EXECUTORS.get(ftype)
    if executor is None:
        return {"executed": False, "detail": f"No executor for finding_type '{ftype}'."}
    return executor(finding)


# ---------------------------------------------------------------------------
# Pipeline step entry point
# ---------------------------------------------------------------------------

def run(ctx: PipelineContext, *, dry_run: bool = False) -> dict:
    """
    Process all pending findings of auto-executable types.

    If ``dry_run`` is True, reports what would happen without mutating.

    Returns {"executed": int, "skipped": int, "errors": int, "details": [...]}.
    """
    from app.assistant.kg_maintenance.store import get_findings, set_status

    executed = 0
    skipped = 0
    errors = 0
    details: list[dict] = []

    for ftype in sorted(AUTO_EXECUTE_TYPES):
        findings = get_findings(status="approved", finding_type=ftype, limit=500)
        logger.info("[execute_findings] %d pending %s findings", len(findings), ftype)

        for finding in findings:
            fid = finding["id"]

            if dry_run:
                details.append({"finding_id": fid, "type": ftype, "dry_run": True, "action": finding.get("suggested_action")})
                skipped += 1
                continue

            try:
                result = execute_single_finding(finding)
            except Exception as exc:
                logger.error("[execute_findings] Failed finding_id=%s: %s", fid, exc)
                logger.debug("[execute_findings] Exception details", exc_info=True)
                set_status(fid, "execute_error", executed_by="auto_pipeline", execution_notes=str(exc)[:500])
                errors += 1
                details.append({"finding_id": fid, "type": ftype, "error": str(exc)[:200]})
                continue

            if result.get("executed"):
                set_status(fid, "executed", executed_by="auto_pipeline", execution_notes=result.get("detail", ""))
                executed += 1
            else:
                set_status(fid, "rejected", executed_by="auto_pipeline", execution_notes=result.get("detail", ""))
                skipped += 1

            details.append({"finding_id": fid, "type": ftype, **result})

    logger.info(
        "[execute_findings] Done: executed=%d skipped=%d errors=%d",
        executed, skipped, errors,
    )
    return {
        "executed": executed,
        "skipped": skipped,
        "errors": errors,
        "details": details[:100],
    }
