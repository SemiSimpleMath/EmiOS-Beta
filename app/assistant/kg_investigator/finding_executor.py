"""Consumer for kg_maintenance_finding rows that already carry an
investigation_report_json. Picks up findings whose grace window has
expired, hands the recommendation prose to ``kg_resolution_manager``,
and lets the manager apply the mutation.

Two entry points:
- execute_one(finding_id): one finding by id (manual / Accept-button use)
- run_executable_findings(limit=N): bounded sweep for routine wiring,
  filters to disposition='auto_apply' AND past 24h grace.

Idempotent: only operates on rows where
  status = 'investigated'
  AND investigation_report_json IS NOT NULL
  AND disposition = 'auto_apply'
  AND investigated_at < now - 24h  (for the routine path)

The ``execute_one`` path bypasses the 24h gate (used when a dev clicks
Accept on the dev page); the routine path enforces it (auto-apply
catches up findings the dev didn't review).

After the manager runs, status flips to 'executed' / 'escalated' /
'dismissed' via the resolution manager's own report. A second sweep
won't pick up the same row.
"""
from __future__ import annotations

import json as _json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
    ScopeWritePolicy,
)
from app.assistant.utils.time_utils import utc_now
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

MANAGER_NAME = "kg_resolution_manager"
GRACE_WINDOW_HOURS = 24


def _executor_scope() -> ScopeContext:
    """Permissive scope so the resolution manager's scope_contract can
    keep ``write_kg=True``. The manager's own scope_contract still
    narrows tools to its curated allowlist (kg_query, kg_close_state,
    kg_update_node_field — no regen tools, no merge)."""
    return ScopeContext(
        scope_id="scope::kg_investigator::finding_executor",
        owner_id="jukka",
        actor_id="kg_finding_executor",
        surface="system",
        room_id=None,
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        writes=ScopeWritePolicy(write_kg=True, write_unified_log=True),
    )


def _claim_executable_finding_ids(*, limit: int) -> List[str]:
    """Pick the oldest auto-apply investigated rows whose 24h grace expired.

    Filters:
    - status='investigated' (investigator wrote a report)
    - investigation_report_json non-null
    - disposition='auto_apply' (not 'needs_user_review' — those wait for the user)
    - investigated_at older than now - 24h (grace expired)
    """
    cutoff = utc_now() - timedelta(hours=GRACE_WINDOW_HOURS)
    with get_db_manager().read_session() as session:
        rows = (
            session.query(KGMaintenanceFinding.id, KGMaintenanceFinding.investigation_report_json)
            .filter(KGMaintenanceFinding.status == "investigated")
            .filter(KGMaintenanceFinding.investigation_report_json.isnot(None))
            .filter(KGMaintenanceFinding.investigated_at < cutoff)
            .order_by(KGMaintenanceFinding.investigated_at.asc())
            .all()
        )

    # disposition lives inside the JSON blob — filter in Python.
    out: List[str] = []
    for fid, report_json in rows:
        report = report_json
        if isinstance(report, str):
            try:
                report = _json.loads(report)
            except Exception:
                continue
        if not isinstance(report, dict):
            continue
        if (report.get("disposition") or "").strip() != "auto_apply":
            continue
        out.append(fid)
        if len(out) >= limit:
            break
    return out


def _build_brief(finding_id: str) -> Optional[Dict[str, Any]]:
    """Build (task, information) for kg_resolution_manager.

    The manager's planner takes prose and acts on it. The brief carries:
    - the recommendation prose verbatim (load-bearing)
    - the finding's id + type + ids for context
    - the diagnosis + evidence so the planner can verify if it wants to

    The task tells the planner: do exactly what the recommendation says,
    don't reinvestigate from scratch.
    """
    with get_db_manager().read_session() as session:
        f = session.query(KGMaintenanceFinding).filter(KGMaintenanceFinding.id == finding_id).first()
        if f is None:
            return None
        report = f.investigation_report_json or {}
        if isinstance(report, str):
            try:
                report = _json.loads(report)
            except Exception:
                return None
        primary_id = f.primary_node_id
        secondary_id = f.secondary_node_id
        edge_id = f.edge_id
        finding_type = f.finding_type
        priority = f.priority

    recommendation = (report.get("recommendation") or "").strip()
    if not recommendation:
        return None

    diagnosis = (report.get("diagnosis") or "").strip()
    evidence = report.get("evidence") or []
    confidence = report.get("confidence")

    info_parts: List[str] = []
    info_parts.append(f"## Finding")
    info_parts.append(f"- finding_id: `{finding_id}`")
    info_parts.append(f"- finding_type: {finding_type}")
    info_parts.append(f"- priority: {priority}")
    info_parts.append(f"- primary_node_id: `{primary_id}`")
    if secondary_id:
        info_parts.append(f"- secondary_node_id: `{secondary_id}`")
    if edge_id:
        info_parts.append(f"- edge_id: `{edge_id}`")
    info_parts.append(f"- investigator confidence: {confidence}")
    info_parts.append("")

    info_parts.append("## Investigator's recommendation (the contract — execute this)")
    info_parts.append(recommendation)
    info_parts.append("")

    if diagnosis:
        info_parts.append("## Investigator's diagnosis (background)")
        info_parts.append(diagnosis)
        info_parts.append("")

    if evidence:
        info_parts.append("## Evidence the investigator cited")
        for e in evidence[:10]:
            q = (e.get("query") if isinstance(e, dict) else "") or ""
            ff = (e.get("finding") if isinstance(e, dict) else "") or ""
            info_parts.append(f"- query: {q}")
            info_parts.append(f"  -> {ff}")
        info_parts.append("")

    task = (
        f"Apply the investigator's recommendation for finding {finding_id} verbatim. "
        f"The recommendation prose above is the contract — do exactly what it says, "
        f"citing the specific node ids it names. Do NOT reinvestigate from scratch; "
        f"the investigator already did that. If the recommendation turns out to be "
        f"wrong (data has shifted, ids don't exist), escalate rather than improvise."
    )
    return {"task": task, "information": "\n".join(info_parts)}


def _extract_outcome_from_audit(blackboard) -> Optional[Dict[str, Any]]:
    """Pull the structured outcome from the kg_resolution::final_answer audit message."""
    try:
        msgs = blackboard.get_messages()
    except Exception as e:
        logger.warning("could not read blackboard messages: %s", e)
        return None
    for m in msgs:
        sender = str(getattr(m, "sender", "") or "")
        if sender.endswith("final_answer") and "kg_resolution" in sender:
            data = getattr(m, "data", None) or {}
            keep = (
                "summary", "completed", "mutations", "regenerations",
                "findings_resolved", "open_questions",
                "final_answer_answer", "result_summary",
            )
            return {k: data.get(k) for k in keep if data.get(k) is not None} or None
    return None


def execute_one(finding_id: str) -> Dict[str, Any]:
    """Execute a single investigated finding by id.

    Used by:
    - Accept button on the dev page (immediate run, bypasses 24h gate)
    - run_executable_findings cron loop (which has already filtered for grace expiry)
    """
    brief = _build_brief(finding_id)
    if brief is None:
        return {"status": "not_found_or_no_recommendation", "finding_id": finding_id}

    mgr = DI.multi_agent_manager_factory.create_manager(MANAGER_NAME)
    if mgr is None:
        return {"status": "error", "finding_id": finding_id,
                "error": f"manager {MANAGER_NAME} not registered"}

    msg = Message(
        task=brief["task"],
        information=brief["information"],
        scope_context=_executor_scope(),
    )
    try:
        DI.manager_invoker.invoke(mgr, msg)
    except Exception as e:
        logger.error("resolution manager invocation failed for finding %s: %s", finding_id, e)
        logger.debug("resolution manager exception details", exc_info=True)
        return {"status": "error", "finding_id": finding_id, "error": str(e)}

    outcome = _extract_outcome_from_audit(mgr.blackboard) or {}
    return {
        "status": "ran",
        "finding_id": finding_id,
        "outcome": outcome,
    }


def run_executable_findings(*, limit: int = 5) -> Dict[str, Any]:
    """Pick up auto-apply investigated findings whose 24h grace has expired
    and execute them sequentially. Wired to the kg_finding_executor_drain
    routine (03:45 daily)."""
    ids = _claim_executable_finding_ids(limit=limit)
    if not ids:
        return {"status": "no_executable_findings", "processed": 0, "results": []}

    results: List[Dict[str, Any]] = []
    ran = errors = 0
    for fid in ids:
        r = execute_one(fid)
        results.append(r)
        if r.get("status") == "ran":
            ran += 1
        else:
            errors += 1

    logger.info(
        "[finding_executor] processed=%d ran=%d errors=%d",
        len(ids), ran, errors,
    )
    return {
        "status": "ok",
        "processed": len(ids),
        "ran": ran,
        "errors": errors,
        "results": results,
    }
