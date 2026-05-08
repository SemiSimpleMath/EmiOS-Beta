"""Loop pending kg_maintenance_finding rows through kg_investigation_manager.

For each pending finding:
  1. Build an investigator brief (finding_brief.build_finding_brief).
  2. Invoke kg_investigation_manager with the brief.
  3. Pull the structured report from the final-answer agent's audit message.
  4. Persist the report into kg_maintenance_finding.investigation_report_json
     and bump status to 'investigated'.

Idempotent: skips findings whose status is not 'pending'.

Wireable to a routine: ``run_pending_findings(limit=N)`` returns a small
result dict the routine can write to its day file.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg_investigator.finding_brief import build_finding_brief
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
    ScopeWritePolicy,
)
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

MANAGER_NAME = "kg_investigation_manager"
MUTATION_MANAGER_NAME = "kg_mutation_manager"


def _investigation_scope() -> ScopeContext:
    """Scope context for the kg_investigation_manager.

    The investigator reads the KG / wiki / evidence and writes a structured
    report onto the kg_maintenance_finding row (status: pending -> investigated).
    It does NOT mutate KG nodes/edges — that's the executor's job after the
    user approves the finding. So write_kg stays False here.

    Strict scope mode requires every Message at manager ingress to carry a
    scope_context; without this the manager refuses ingress with
    "Missing scope_context at manager ingress while strict scope mode is
    enabled". Both entry points (the pipeline-internal _investigate_findings_for_run
    and the on-demand /kg-maintenance/api/finding/<id>/investigate route)
    pass through investigate_one(), so building the scope here covers both.
    """
    return ScopeContext(
        scope_id="scope::kg_investigator::finding_processor",
        owner_id="jukka",
        actor_id="kg_finding_investigator",
        surface="system",
        room_id=None,
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        writes=ScopeWritePolicy(write_kg=False, write_unified_log=False),
    )


def _mutation_scope() -> ScopeContext:
    """Scope context for the kg_mutation_manager.

    The mutator's purpose is to apply a previously-recorded proposed_action to
    the KG via narrow audit-wired tools (kg_merge_nodes, kg_rename_label,
    kg_update_node_field, kg_finding_resolve, kg_finding_escalate). Therefore
    write_kg is True here.
    """
    return ScopeContext(
        scope_id="scope::kg_investigator::finding_executor",
        owner_id="jukka",
        actor_id="kg_finding_executor",
        surface="system",
        room_id=None,
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        writes=ScopeWritePolicy(write_kg=True, write_unified_log=False),
    )


def _build_apply_brief(finding_id: str) -> Optional[tuple[str, str]]:
    """Build (task, information) for the mutation_manager planner from a
    finding's investigation report. Returns None if the finding is missing,
    not investigated, or has no structured report.

    Operator notes (saved on the finding before clicking Apply) are
    surfaced prominently in the task string. The planner prompt renders
    {{ task }} at the top, so notes there are read first; embedding them
    only inside the JSON information blob risks the planner skimming past.
    """
    import json
    with get_db_manager().read_session() as session:
        f = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.id == finding_id)
            .first()
        )
        if f is None:
            return None
        if f.status != "investigated":
            return None
        report = f.investigation_report_json
        if report is None:
            return None
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except Exception:
                return None
        finding_view = {
            "finding_id": f.id,
            "finding_type": f.finding_type,
            "primary_node_id": f.primary_node_id,
            "secondary_node_id": f.secondary_node_id,
            "edge_id": f.edge_id,
            "suggested_action": f.suggested_action,
            "reason": f.reason,
            "priority": f.priority,
        }

    operator_notes = ""
    if isinstance(report, dict):
        raw_notes = report.get("operator_notes")
        if isinstance(raw_notes, str):
            operator_notes = raw_notes.strip()

    task_parts = [
        f"Apply the proposed_action from finding {finding_id} per the "
        "investigation report. Follow the kg_mutation::planner decision rules "
        "(no_action / escalate / apply mutation+resolve).",
    ]
    if operator_notes:
        task_parts.append(
            "OPERATOR NOTES (added by the human reviewer before clicking "
            "Apply — weight these against the investigator's proposed_action; "
            "they may override, refine, or veto it):"
        )
        task_parts.append(operator_notes)
    task = "\n\n".join(task_parts)

    information = json.dumps(
        {"finding": finding_view, "investigation_report": report},
        indent=2,
        default=str,
    )
    return task, information


def apply_one(finding_id: str) -> Dict[str, Any]:
    """Hand an investigated finding to kg_mutation_manager so its planner can
    apply the proposed_action (or escalate) per the report's decision rules.

    Returns ``{"status": "applied" | "no_report" | "wrong_status" |
    "not_found" | "error", "finding_id": ..., "result_summary": ..., ...}``.
    The terminal finding-row status (executed / rejected / escalated) is
    written by the planner via ``kg_finding_resolve`` /
    ``kg_finding_escalate`` — we don't touch it directly here.
    """
    import json

    brief = _build_apply_brief(finding_id)
    if brief is None:
        with get_db_manager().read_session() as session:
            f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        if f is None:
            return {"status": "not_found", "finding_id": finding_id}
        if f.status != "investigated":
            return {
                "status": "wrong_status",
                "finding_id": finding_id,
                "current_status": f.status,
            }
        return {"status": "no_report", "finding_id": finding_id}

    task, information = brief
    mgr = DI.multi_agent_manager_factory.create_manager(MUTATION_MANAGER_NAME)
    msg = Message(task=task, information=information, scope_context=_mutation_scope())
    try:
        DI.manager_invoker.invoke(mgr, msg)
    except Exception as e:
        logger.error("apply failed for finding %s: %s", finding_id, e)
        logger.debug("apply exception details", exc_info=True)
        return {"status": "error", "finding_id": finding_id, "error": str(e)}

    # Read back the finding's terminal state so the caller can see what the
    # planner actually did (executed / rejected / escalated / unchanged).
    with get_db_manager().read_session() as session:
        f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        terminal_status = f.status if f else None
        execution_notes = f.execution_notes if f else None

    # Pull a result summary from the mutation_manager's final_answer if there is one.
    result_summary: Optional[str] = None
    try:
        msgs = mgr.blackboard.get_messages()
        for m in msgs:
            sender = str(getattr(m, "sender", "") or "")
            if sender.endswith("final_answer") and "kg_mutation" in sender:
                data = getattr(m, "data", None) or {}
                result_summary = (
                    data.get("result_summary")
                    or data.get("final_answer_answer")
                    or data.get("diagnosis")
                )
                if isinstance(result_summary, str):
                    result_summary = result_summary[:600]
                break
    except Exception as e:
        logger.debug("could not extract final_answer from mutation manager: %s", e)

    return {
        "status": "applied",
        "finding_id": finding_id,
        "terminal_status": terminal_status,
        "execution_notes": execution_notes,
        "result_summary": result_summary,
    }


def _claim_pending_finding_ids(*, limit: int, finding_types: Optional[List[str]] = None) -> List[str]:
    """Pick up to `limit` oldest pending findings, optionally filtered to specific types."""
    with get_db_manager().read_session() as session:
        q = (
            session.query(KGMaintenanceFinding.id)
            .filter(KGMaintenanceFinding.status == "pending")
        )
        if finding_types:
            q = q.filter(KGMaintenanceFinding.finding_type.in_(finding_types))
        rows = q.order_by(KGMaintenanceFinding.created_at.asc()).limit(limit).all()
        return [r[0] for r in rows]


def _extract_report_from_audit(blackboard) -> Optional[Dict[str, Any]]:
    """Pull the structured report from the kg_investigation::final_answer audit message."""
    try:
        msgs = blackboard.get_messages()
    except Exception as e:
        logger.warning("could not read blackboard messages: %s", e)
        return None
    for m in msgs:
        sender = str(getattr(m, "sender", "") or "")
        if sender.endswith("final_answer") and "kg_investigation" in sender:
            data = getattr(m, "data", None) or {}
            # Keep only the structured fields + the markdown rendering. Drop
            # the standard envelope filler. Field list reflects the
            # post-2026-05-07 schema (recommendation prose + disposition
            # routing); keeps `proposed_action` / `affected_records` in the
            # whitelist for any in-flight legacy-shape reads but the new
            # investigator no longer emits them.
            report = {
                k: data.get(k)
                for k in (
                    # New (load-bearing for the executor + UI):
                    "take_action",
                    "verdict_type",
                    "verdict_memo",
                    "verdict_node_ids",
                    "recommendation",
                    "disposition",
                    "user_question",
                    "confidence",
                    # Detail / dev-page rendering:
                    "diagnosis",
                    "evidence",
                    "open_questions",
                    "final_answer_answer",
                    "result_summary",
                    # Legacy fallback (kept so old in-flight rows roundtrip):
                    "proposed_action",
                    "affected_records",
                )
                if data.get(k) is not None
            }
            return report or None
    return None


def _persist_report(*, finding_id: str, report: Dict[str, Any]) -> None:
    with get_db_manager().transaction(op="finding_processor.persist_report") as session:
        f = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.id == finding_id)
            .first()
        )
        if f is None:
            logger.warning("persist_report: finding %s vanished", finding_id)
            return
        f.investigation_report_json = report
        f.investigated_at = datetime.now(timezone.utc)
        f.status = "investigated"


def investigate_one(finding_id: str) -> Dict[str, Any]:
    """Investigate a single finding by id. Returns a small result dict."""
    brief = build_finding_brief(finding_id)
    if brief is None:
        return {"status": "not_found", "finding_id": finding_id}
    task, information = brief

    mgr = DI.multi_agent_manager_factory.create_manager(MANAGER_NAME)
    msg = Message(task=task, information=information, scope_context=_investigation_scope())
    try:
        DI.manager_invoker.invoke(mgr, msg)
    except Exception as e:
        logger.error("investigation failed for finding %s: %s", finding_id, e)
        logger.debug("investigation exception details", exc_info=True)
        return {"status": "error", "finding_id": finding_id, "error": str(e)}

    report = _extract_report_from_audit(mgr.blackboard)
    if report is None:
        logger.warning("no structured report produced for finding %s", finding_id)
        return {"status": "no_report", "finding_id": finding_id}

    _persist_report(finding_id=finding_id, report=report)

    # take_action=False short-circuits the executor path. The investigator
    # has reached a verdict ("leave it alone"); flip directly to 'dismissed'
    # so the finding doesn't sit for 24h and then no-op-escalate. The
    # verdict is recorded in kg_node_verdict for future agents to consult.
    if report.get("take_action") is False:
        from app.assistant.kg_maintenance.store import set_status
        from app.assistant.kg_maintenance.verdict_store import record_verdict
        memo = (report.get("verdict_memo") or "").strip()
        verdict_type = (report.get("verdict_type") or "").strip()
        verdict_node_ids = report.get("verdict_node_ids") or []
        if not isinstance(verdict_node_ids, list):
            verdict_node_ids = []
        # Fall back to the finding's own subject node ids if the
        # investigator didn't fill verdict_node_ids — keeps the verdict
        # discoverable even for older agent runs.
        if not verdict_node_ids:
            with get_db_manager().read_session() as session:
                f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
                if f is not None:
                    verdict_node_ids = [
                        nid for nid in (f.primary_node_id, f.secondary_node_id) if nid
                    ]

        verdict_id = None
        if verdict_type and memo and verdict_node_ids:
            verdict_id = record_verdict(
                verdict_type=verdict_type,
                memo=memo,
                node_ids=verdict_node_ids,
                decided_by="agent:kg_investigation",
                reasoning=report.get("recommendation"),
                source_finding_id=finding_id,
                confidence=report.get("confidence"),
            )
        else:
            logger.warning(
                "[finding_processor] take_action=False but missing verdict_type/"
                "memo/node_ids on finding %s — verdict not recorded",
                finding_id,
            )

        notes = f"Investigator verdict: no action needed. Memo: {memo}" if memo else "Investigator verdict: no action needed."
        if verdict_id:
            notes += f" (verdict_id={verdict_id})"
        set_status(
            finding_id,
            "dismissed",
            executed_by="agent:kg_investigation",
            execution_notes=notes,
        )
        return {
            "status": "dismissed",
            "finding_id": finding_id,
            "verdict_id": verdict_id,
            "verdict_type": verdict_type,
            "verdict_memo": memo,
            "summary": report.get("result_summary") or memo,
        }

    summary = report.get("result_summary") or (report.get("diagnosis") or "")[:140]
    proposed = report.get("proposed_action") or {}
    op = proposed.get("op") if isinstance(proposed, dict) else None
    return {
        "status": "investigated",
        "finding_id": finding_id,
        "proposed_op": op,
        "summary": summary,
    }


def drain_pending_findings(
    *,
    limit: int = 5,
    finding_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Drain the oldest `limit` pending findings from the backlog (FIFO).

    Routine entry-point — different from the pipeline's
    ``_investigate_findings_for_run`` which only sees that run's own findings.
    This function picks from the global pending queue regardless of which
    pipeline run produced them, so old findings don't sit forever.

    Optional ``finding_types`` filter restricts to specific types
    (e.g. ['duplicate_node', 'orphan_node']).
    """
    ids = _claim_pending_finding_ids(limit=limit, finding_types=finding_types)
    if not ids:
        logger.info("[finding_processor] drain: no pending findings")
        return {"status": "empty_queue", "processed": 0, "results": []}
    logger.info("[finding_processor] drain: investigating %d oldest pending findings", len(ids))
    return investigate_findings(ids, max_to_investigate=limit)


def investigate_findings(
    finding_ids: List[str],
    *,
    max_to_investigate: int = 50,
) -> Dict[str, Any]:
    """Investigate an explicit list of finding ids — typically returned by a
    producer (wiki critic, etc.) right after it wrote them.

    Skips ids that aren't pending (e.g., already investigated or rejected).
    Caps total work at ``max_to_investigate`` to keep producer routines bounded.
    """
    ids = list(dict.fromkeys((fid or "").strip() for fid in (finding_ids or []) if fid))
    if not ids:
        return {"status": "no_ids", "processed": 0, "results": []}

    # Filter to ids that are still pending — others (already investigated /
    # rejected / executed) shouldn't be redone.
    with get_db_manager().read_session() as session:
        rows = (
            session.query(KGMaintenanceFinding.id)
            .filter(KGMaintenanceFinding.id.in_(ids))
            .filter(KGMaintenanceFinding.status == "pending")
            .all()
        )
        pending_ids = [r[0] for r in rows]

    if not pending_ids:
        return {"status": "nothing_pending", "processed": 0, "results": []}

    if len(pending_ids) > max_to_investigate:
        logger.info(
            "[finding_processor] investigate_findings capped %d → %d",
            len(pending_ids), max_to_investigate,
        )
        pending_ids = pending_ids[:max_to_investigate]

    results: List[Dict[str, Any]] = []
    investigated = 0
    errors = 0
    for fid in pending_ids:
        r = investigate_one(fid)
        results.append(r)
        if r.get("status") == "investigated":
            investigated += 1
        elif r.get("status") in ("error", "no_report"):
            errors += 1

    logger.info(
        "[finding_processor] processed=%d investigated=%d errors=%d (from %d ids)",
        len(pending_ids), investigated, errors, len(ids),
    )
    return {
        "status": "ok",
        "processed": len(pending_ids),
        "investigated": investigated,
        "errors": errors,
        "results": results,
    }


def run_pending_findings(
    *,
    limit: int = 5,
    finding_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Process up to `limit` pending findings sequentially. Optionally filter
    to specific finding_types (e.g., ['wiki_contradiction'])."""
    ids = _claim_pending_finding_ids(limit=limit, finding_types=finding_types)
    if not ids:
        return {"status": "no_pending_findings", "processed": 0, "results": []}

    results: List[Dict[str, Any]] = []
    investigated = 0
    errors = 0
    for fid in ids:
        r = investigate_one(fid)
        results.append(r)
        if r.get("status") == "investigated":
            investigated += 1
        elif r.get("status") in ("error", "no_report"):
            errors += 1

    logger.info(
        "[finding_processor] processed=%d investigated=%d errors=%d",
        len(ids), investigated, errors,
    )
    return {
        "status": "ok",
        "processed": len(ids),
        "investigated": investigated,
        "errors": errors,
        "results": results,
    }
