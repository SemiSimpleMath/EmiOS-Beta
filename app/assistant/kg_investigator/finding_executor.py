"""Consumer for kg_maintenance_finding rows that already carry an
investigation_report_json. Picks them up, hands the report to
``kg_mutation_manager``, and lets the manager decide whether to apply
the mutation or escalate.

execute_one(finding_id) — one finding by id (manual / one-shot use)
run_executable_findings(limit=N) — bounded sweep for routine wiring

Idempotent: only operates on rows where
  status = 'investigated' AND investigation_report_json IS NOT NULL.
The manager updates status in-place via kg_finding_resolve /
kg_finding_escalate, so a second run won't pick the same row up.

Per-call cap (max_to_execute) bounds LLM cost when wiring this into
a routine. The mutation manager caps its own internal loop too.
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Optional

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
from app.models.db_manager import get_db_manager


def _mutation_scope() -> ScopeContext:
    """Scope context that authorizes the mutation manager to write to the KG.

    The system refuses to widen ``writes.write_kg`` from False to True via
    the manager's scope_contract; the inbound Message has to already grant
    the right. So the executor seeds a permissive scope here. The manager's
    own scope_contract still narrows tools to the typed mutator allowlist.
    """
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

logger = get_logger(__name__)

MANAGER_NAME = "kg_mutation_manager"


def _claim_executable_finding_ids(*, limit: int) -> List[str]:
    """Pick the oldest investigated rows that still need execution."""
    with get_db_manager().read_session() as session:
        rows = (
            session.query(KGMaintenanceFinding.id)
            .filter(KGMaintenanceFinding.status == "investigated")
            .filter(KGMaintenanceFinding.investigation_report_json.isnot(None))
            .order_by(KGMaintenanceFinding.investigated_at.asc())
            .limit(limit)
            .all()
        )
        return [r[0] for r in rows]


def _build_brief(finding_id: str) -> Optional[Dict[str, Any]]:
    """Produce (task, information) for the mutation manager.

    The mutation planner needs the report's structured proposed_action
    plus enough finding context (label, types, ids) to call the right
    typed tool. Everything is passed as text on the Message so the
    planner agent reads it like any other task.
    """
    with get_db_manager().read_session() as session:
        f = session.query(KGMaintenanceFinding).filter(KGMaintenanceFinding.id == finding_id).first()
        if f is None:
            return None
        report = f.investigation_report_json or {}
        proposed_action = report.get("proposed_action") or {}

        primary_id = f.primary_node_id
        secondary_id = f.secondary_node_id
        edge_id = f.edge_id

    op = (proposed_action.get("op") or "").strip()
    confidence = proposed_action.get("confidence")
    reversibility = proposed_action.get("reversibility") or "unknown"
    args_text = proposed_action.get("args") or ""

    diagnosis = (report.get("diagnosis") or "").strip()
    evidence = report.get("evidence") or []
    open_questions = report.get("open_questions") or []

    info_parts: List[str] = []
    info_parts.append(f"## Finding")
    info_parts.append(f"- finding_id: `{finding_id}`")
    info_parts.append(f"- finding_type: {f.finding_type}")
    info_parts.append(f"- priority: {f.priority}")
    info_parts.append(f"- primary_node_id: `{primary_id}`")
    if secondary_id:
        info_parts.append(f"- secondary_node_id: `{secondary_id}`")
    if edge_id:
        info_parts.append(f"- edge_id: `{edge_id}`")
    info_parts.append("")

    info_parts.append(f"## Investigator's diagnosis")
    info_parts.append(diagnosis or "(empty)")
    info_parts.append("")

    if evidence:
        info_parts.append(f"## Evidence cited")
        for e in evidence:
            q = (e.get("query") if isinstance(e, dict) else "") or ""
            ff = (e.get("finding") if isinstance(e, dict) else "") or ""
            info_parts.append(f"- query: {q[:160]}")
            info_parts.append(f"  -> {ff[:240]}")
        info_parts.append("")

    info_parts.append(f"## Proposed action (the contract)")
    info_parts.append(f"- op: `{op or 'unknown'}`")
    info_parts.append(f"- args: {args_text}")
    info_parts.append(f"- reversibility: {reversibility}")
    info_parts.append(f"- confidence: {confidence}")
    info_parts.append("")

    if open_questions:
        info_parts.append(f"## Open questions (not blocking, surfaced for context)")
        for q in open_questions:
            info_parts.append(f"- {q}")
        info_parts.append("")

    task = (
        f"Apply the investigator's proposed_action for finding {finding_id}, "
        f"or escalate to user review per your decision rules. The op is `{op or 'unknown'}` "
        f"with reversibility={reversibility} confidence={confidence}. Do NOT improvise a "
        f"different mutation than what the investigator proposed."
    )
    return {"task": task, "information": "\n".join(info_parts)}


def _extract_outcome_from_audit(blackboard) -> Optional[Dict[str, Any]]:
    """Pull the structured outcome from the kg_mutation::final_answer audit message."""
    try:
        msgs = blackboard.get_messages()
    except Exception as e:
        logger.warning("could not read blackboard messages: %s", e)
        return None
    for m in msgs:
        sender = str(getattr(m, "sender", "") or "")
        if sender.endswith("final_answer") and "kg_mutation" in sender:
            data = getattr(m, "data", None) or {}
            keep = (
                "outcome", "op_applied", "revision_log_id", "finding_status",
                "error_message", "final_answer_answer", "result_summary",
            )
            return {k: data.get(k) for k in keep if data.get(k) is not None} or None
    return None


def execute_one(finding_id: str) -> Dict[str, Any]:
    """Execute a single investigated finding by id."""
    brief = _build_brief(finding_id)
    if brief is None:
        return {"status": "not_found", "finding_id": finding_id}

    mgr = DI.multi_agent_manager_factory.create_manager(MANAGER_NAME)
    msg = Message(
        task=brief["task"],
        information=brief["information"],
        scope_context=_mutation_scope(),
    )
    try:
        DI.manager_invoker.invoke(mgr, msg)
    except Exception as e:
        logger.error("mutation manager invocation failed for finding %s: %s", finding_id, e)
        logger.debug("mutation manager exception details", exc_info=True)
        return {"status": "error", "finding_id": finding_id, "error": str(e)}

    outcome = _extract_outcome_from_audit(mgr.blackboard) or {}
    return {
        "status": "ran",
        "finding_id": finding_id,
        "outcome": outcome,
    }


def run_executable_findings(*, limit: int = 5) -> Dict[str, Any]:
    """Pick up the oldest investigated findings and execute them sequentially."""
    ids = _claim_executable_finding_ids(limit=limit)
    if not ids:
        return {"status": "no_executable_findings", "processed": 0, "results": []}

    results: List[Dict[str, Any]] = []
    applied = escalated = no_action = errors = 0
    for fid in ids:
        r = execute_one(fid)
        results.append(r)
        outcome = (r.get("outcome") or {}).get("outcome")
        if outcome == "applied":
            applied += 1
        elif outcome == "escalated":
            escalated += 1
        elif outcome == "no_action":
            no_action += 1
        else:
            errors += 1

    logger.info(
        "[finding_executor] processed=%d applied=%d escalated=%d no_action=%d errors=%d",
        len(ids), applied, escalated, no_action, errors,
    )
    return {
        "status": "ok",
        "processed": len(ids),
        "applied": applied,
        "escalated": escalated,
        "no_action": no_action,
        "errors": errors,
        "results": results,
    }
