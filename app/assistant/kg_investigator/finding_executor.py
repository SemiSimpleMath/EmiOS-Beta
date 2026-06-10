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

from sqlalchemy import text as sql_text

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.database.kg_revision_log import KGRevisionLog
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.identity_names import PRINCIPAL_USER
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeContext
from app.assistant.utils.time_utils import utc_now
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

MANAGER_NAME = "kg_resolution_manager"
GRACE_WINDOW_HOURS = 24

# A claim ('executing') older than this is assumed orphaned by a process
# crash mid-run and is released back to 'investigated' on the next sweep.
STALE_CLAIM_HOURS = 2


def _executor_scope() -> ScopeContext:
    """Permissive scope so the resolution manager's scope_contract can
    keep ``write_kg=True``. The manager's own scope_contract narrows
    tools to its curated mutator allowlist (full read + mutate suite
    including merge/delete; safety lives in the dev-page 24h grace +
    Accept review, not in tool exclusion)."""
    return load_scope_for_source(
        kind="subsystem",
        source_id="kg_investigator",
        actor_id="kg_finding_executor",
        identity_overrides={
            "owner_id": PRINCIPAL_USER,
            "actor_id": "kg_finding_executor",
            "scope_id": "scope::kg_investigator::finding_executor",
        },
    )


def _claim_executable_finding_ids(*, limit: int) -> List[str]:
    """Pick auto-apply investigated rows whose 24h grace expired,
    ordered by max importance of the touched nodes (high first).

    Filters:
    - status='investigated' (investigator wrote a report)
    - investigation_report_json non-null
    - disposition='auto_apply' (not 'needs_user_review' — those wait for the user)
    - investigated_at older than now - 24h (grace expired)
    - superseded_by IS NULL (siblings close via their cluster lead's cascade)

    Wedding-vs-backpack ordering: a finding's importance is the max
    importance across its primary node and one-hop neighbors. Date is
    the tie-breaker (oldest first within an importance tier).
    """
    from app.assistant.kg_investigator.finding_priority import order_by_importance

    cutoff = utc_now() - timedelta(hours=GRACE_WINDOW_HOURS)
    with get_db_manager().read_session() as session:
        rows = (
            session.query(
                KGMaintenanceFinding.id,
                KGMaintenanceFinding.investigation_report_json,
                KGMaintenanceFinding.primary_node_id,
                KGMaintenanceFinding.investigated_at,
            )
            .filter(KGMaintenanceFinding.status == "investigated")
            .filter(KGMaintenanceFinding.investigation_report_json.isnot(None))
            .filter(KGMaintenanceFinding.investigated_at < cutoff)
            # Superseded siblings are closed by their cluster lead's
            # cascade — executing them independently double-applies
            # against the same root cause.
            .filter(KGMaintenanceFinding.superseded_by.is_(None))
            .all()
        )

    # disposition lives inside the JSON blob — filter in Python.
    eligible: List[tuple] = []  # (fid, primary_node_id, investigated_at)
    for fid, report_json, primary_node_id, investigated_at in rows:
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
        eligible.append((fid, primary_node_id, investigated_at))

    # Sort by max importance of touched nodes (high first), date as
    # tie-breaker (oldest first), then take the top `limit`.
    ordered_ids = order_by_importance(eligible)
    return ordered_ids[:limit]


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

    operator_notes = (report.get("operator_notes") or "").strip()

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

    if operator_notes:
        # Operator directions override; the recommendation is moot. Replace
        # the prose with a stub so the planner doesn't waste tokens parsing
        # a position the operator just rejected.
        info_parts.append("## Investigator's recommendation")
        info_parts.append("(superseded — see OPERATOR DIRECTIONS below)")
    else:
        info_parts.append("## Investigator's recommendation (the contract — execute this)")
        info_parts.append(recommendation)
    info_parts.append("")

    if diagnosis and not operator_notes:
        info_parts.append("## Investigator's diagnosis (background)")
        info_parts.append(diagnosis)
        info_parts.append("")

    if evidence and not operator_notes:
        info_parts.append("## Evidence the investigator cited")
        for e in evidence[:10]:
            q = (e.get("query") if isinstance(e, dict) else "") or ""
            ff = (e.get("finding") if isinstance(e, dict) else "") or ""
            info_parts.append(f"- query: {q}")
            info_parts.append(f"  -> {ff}")
        info_parts.append("")

    if operator_notes:
        # Single, terminal block. No top banner, no preamble paragraph —
        # the override semantics live in the task prose; this section just
        # delivers the operator's words.
        info_parts.append("## OPERATOR DIRECTIONS (authoritative)")
        info_parts.append(operator_notes)
        info_parts.append("")

    if operator_notes:
        task = (
            f"Apply finding {finding_id} per the OPERATOR DIRECTIONS section. "
            f"Treat the operator's words as ground truth and execute against them; "
            f"cite specific node ids. The investigator's recommendation has been "
            f"superseded — do not fall back to it. Date convention: if the operator "
            f"gives only month + year, use the first of the month "
            f"(e.g. 'August 1994' → '1994-08-01'); year only → January 1st. "
            f"If a directive cannot be operationalized (ids don't exist, data has "
            f"shifted), escalate rather than improvise."
        )
    else:
        task = (
            f"Apply the investigator's recommendation for finding {finding_id} verbatim. "
            f"The recommendation prose above is the contract — do exactly what it says, "
            f"citing the specific node ids it names. Do NOT reinvestigate from scratch; "
            f"the investigator already did that. If the recommendation turns out to be "
            f"wrong (data has shifted, ids don't exist), escalate rather than improvise."
        )
    return {"task": task, "information": "\n".join(info_parts)}


def _claim_for_execution(finding_id: str) -> bool:
    """Atomic claim: investigated → executing. Returns False when the row
    is anything else (terminal, already claimed by a concurrent caller, or
    still pending) — the caller must skip without touching it. This is the
    single gate that makes double-execution impossible (audit P0.4)."""
    with get_db_manager().transaction(op="finding_executor.claim") as session:
        res = session.execute(
            sql_text(
                "UPDATE kg_maintenance_finding SET status = 'executing' "
                "WHERE id = :fid AND status = 'investigated'"
            ),
            {"fid": finding_id},
        )
        return (res.rowcount or 0) == 1


def _release_claim(finding_id: str) -> None:
    """Return a claimed row to 'investigated' (manager error → retry on a
    later sweep). No-op unless the row is still 'executing'."""
    with get_db_manager().transaction(op="finding_executor.release") as session:
        session.execute(
            sql_text(
                "UPDATE kg_maintenance_finding SET status = 'investigated' "
                "WHERE id = :fid AND status = 'executing'"
            ),
            {"fid": finding_id},
        )


def _current_status(finding_id: str) -> Optional[str]:
    with get_db_manager().read_session() as session:
        row = (
            session.query(KGMaintenanceFinding.status)
            .filter(KGMaintenanceFinding.id == finding_id)
            .first()
        )
    return row[0] if row else None


def _revision_rows_since(finding_id: str, watermark) -> tuple[int, int]:
    """Ground truth for "did the executor actually mutate anything":
    successful kg_revision_log rows written after ``watermark``. Returns
    (tagged, untagged) — tagged rows carry this finding_id; untagged rows
    are mutations the planner applied without passing finding_id."""
    with get_db_manager().read_session() as session:
        tagged = (
            session.query(KGRevisionLog.id)
            .filter(KGRevisionLog.finding_id == finding_id)
            .filter(KGRevisionLog.succeeded == 1)
            .filter(KGRevisionLog.created_at >= watermark)
            .count()
        )
        untagged = (
            session.query(KGRevisionLog.id)
            .filter(KGRevisionLog.finding_id.is_(None))
            .filter(KGRevisionLog.succeeded == 1)
            .filter(KGRevisionLog.created_at >= watermark)
            .count()
        )
    return tagged, untagged


def _drift_reason(finding_id: str) -> Optional[str]:
    """Compare the node snapshot persisted at investigation time against
    the current rows. The 24h grace guarantees the recommendation is at
    least a day stale; if a cited node's substance (label / validity
    window) changed — or the node vanished — executing the old plan is
    unsafe. Returns a human-readable reason, or None when no drift.

    Substance fields only: ``updated_at``/``last_observed`` move on every
    re-observation stamp, so gating on them would escalate perfectly
    valid recommendations about active nodes. (The snapshot still stores
    updated_at for telemetry.)
    """
    with get_db_manager().read_session() as session:
        f = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.id == finding_id)
            .first()
        )
        if f is None:
            return None
        report = f.investigation_report_json or {}
        if isinstance(report, str):
            try:
                report = _json.loads(report)
            except Exception:
                return None
        snap = report.get("node_snapshot_at_investigation")
        if not isinstance(snap, dict) or not snap:
            return None  # pre-drift-guard report; nothing to compare

        drifts: List[str] = []
        for node_id, then in snap.items():
            if not isinstance(then, dict):
                continue
            row = session.execute(
                sql_text(
                    "SELECT label, start_date, end_date FROM kg_node_metadata "
                    "WHERE id = :nid"
                ),
                {"nid": node_id},
            ).fetchone()
            if row is None:
                drifts.append(f"node {node_id[:8]} no longer exists")
                continue
            now_vals = {
                "label": row[0],
                "start_date": str(row[1]) if row[1] is not None else None,
                "end_date": str(row[2]) if row[2] is not None else None,
            }
            for field in ("label", "start_date", "end_date"):
                then_v = then.get(field)
                then_v = str(then_v) if then_v is not None else None
                if then_v != now_vals[field]:
                    drifts.append(
                        f"node {node_id[:8]} {field} changed "
                        f"({then_v!r} → {now_vals[field]!r})"
                    )
    return "; ".join(drifts) if drifts else None


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

    Integrity contract (audit P0.4):
    - Atomic claim investigated → executing; any concurrent second call
      returns ``skipped`` without touching the row.
    - Drift guard: if a cited node's substance changed since investigation,
      escalate as stale_recommendation instead of executing a stale plan.
    - Outcome is decided from kg_revision_log ground truth, not the
      planner's self-report (which stays as telemetry).
    """
    from app.assistant.kg_maintenance.store import set_status

    if not _claim_for_execution(finding_id):
        current = _current_status(finding_id)
        return {
            "status": "skipped",
            "finding_id": finding_id,
            "reason": f"not claimable (status={current!r}; needs 'investigated')",
        }

    try:
        brief = _build_brief(finding_id)
    except Exception:
        _release_claim(finding_id)
        raise
    if brief is None:
        # Claimed, but the report carries no recommendation prose — this
        # row can never execute; park it for human review instead of
        # letting every sweep re-claim it forever.
        set_status(
            finding_id, "escalated",
            executed_by="agent:kg_resolution_executor",
            execution_notes="No recommendation prose in investigation report — cannot execute.",
        )
        return {"status": "escalated", "finding_id": finding_id,
                "reason": "no_recommendation"}

    drift = _drift_reason(finding_id)
    if drift:
        set_status(
            finding_id, "escalated",
            executed_by="agent:kg_resolution_executor",
            execution_notes=f"stale_recommendation: {drift}",
        )
        return {"status": "escalated", "finding_id": finding_id,
                "reason": "stale_recommendation", "drift": drift}

    mgr = DI.multi_agent_manager_factory.create_manager(MANAGER_NAME)
    if mgr is None:
        _release_claim(finding_id)
        return {"status": "error", "finding_id": finding_id,
                "error": f"manager {MANAGER_NAME} not registered"}

    msg = Message(
        task=brief["task"],
        information=brief["information"],
        scope_context=_executor_scope(),
    )
    watermark = utc_now()
    try:
        DI.manager_invoker.invoke(mgr, msg)
    except Exception as e:
        logger.error("resolution manager invocation failed for finding %s: %s", finding_id, e)
        logger.debug("resolution manager exception details", exc_info=True)
        # Release so a later sweep can retry — the claim must not orphan
        # the row on transient manager/LLM failures.
        _release_claim(finding_id)
        return {"status": "error", "finding_id": finding_id, "error": str(e)}

    outcome = _extract_outcome_from_audit(mgr.blackboard) or {}
    mutations = outcome.get("mutations") or []
    regens = outcome.get("regenerations") or []
    summary = outcome.get("result_summary") or outcome.get("summary") or ""

    # A resolution tool (kg_finding_resolve / kg_finding_escalate) may have
    # moved the row itself during the run — respect that; don't overwrite.
    current = _current_status(finding_id)
    if current != "executing":
        return {
            "status": current or "unknown",
            "finding_id": finding_id,
            "outcome": outcome,
            "terminal_status": current,
            "note": "status set by a resolution tool during the run",
        }

    # Ground truth: successful kg_revision_log rows written during this
    # run. The self-reported mutations list is telemetry only — a
    # hallucinated list must never mark 'executed' (audit P0.4).
    tagged, untagged = _revision_rows_since(finding_id, watermark)

    if tagged > 0 or (untagged > 0 and mutations):
        corroboration = (
            f"{tagged} revision-log row(s) tagged with this finding"
            if tagged > 0
            else f"{untagged} untagged revision-log row(s) during the run "
                 f"(planner omitted finding_id)"
        )
        set_status(
            finding_id, "executed",
            executed_by="agent:kg_resolution_executor",
            execution_notes=(
                f"Executor applied mutations — {corroboration}. "
                f"Self-report: {len(mutations)} mutation(s), "
                f"{len(regens)} regeneration(s). Manager said: {summary}"
            ),
        )
        return {
            "status": "executed",
            "finding_id": finding_id,
            "outcome": outcome,
            "terminal_status": "executed",
            "revision_rows": {"tagged": tagged, "untagged": untagged},
        }

    if mutations:
        # Planner CLAIMED mutations but the revision log shows none — the
        # self-report is not corroborated. Never mark executed on word alone.
        set_status(
            finding_id, "escalated",
            executed_by="agent:kg_resolution_executor",
            execution_notes=(
                f"Self-reported {len(mutations)} mutation(s) not corroborated "
                f"by kg_revision_log (0 rows written during the run). "
                f"Manager said: {summary}"
            ),
        )
        return {
            "status": "escalated",
            "finding_id": finding_id,
            "outcome": outcome,
            "terminal_status": "escalated",
            "reason": "self_report_not_corroborated",
        }

    # True no-op: planner declined to operationalize the recommendation
    # (data shifted, ids stale, case ambiguous). Escalate so the finding
    # doesn't sit in 'investigated' forever.
    set_status(
        finding_id, "escalated",
        executed_by="agent:kg_resolution_executor",
        execution_notes=(
            f"Executor ran but applied no mutations — planner declined "
            f"to operationalize the recommendation (data may have "
            f"shifted, ids may be stale, or the case became ambiguous). "
            f"Manager said: {summary}"
        ),
    )
    return {
        "status": "escalated",
        "finding_id": finding_id,
        "outcome": outcome,
        "result_summary": "Escalated — executor's planner declined to mutate. Re-investigate via /kg-dev.",
        "terminal_status": "escalated",
    }


def _reclaim_stale_executions() -> int:
    """Release 'executing' rows older than STALE_CLAIM_HOURS back to
    'investigated'. A claim only sticks this long when the process died
    mid-run; without this sweep such rows would be invisible to every
    queue forever."""
    cutoff = utc_now() - timedelta(hours=STALE_CLAIM_HOURS)
    with get_db_manager().transaction(op="finding_executor.reclaim_stale") as session:
        stale = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.status == "executing")
            .filter(KGMaintenanceFinding.updated_at < cutoff)
            .all()
        )
        for f in stale:
            f.status = "investigated"
            f.execution_notes = (
                f"Stale 'executing' claim released after {STALE_CLAIM_HOURS}h "
                f"(process likely died mid-run). "
                + (f.execution_notes or "")
            )[:500]
        n = len(stale)
    if n:
        logger.warning("[finding_executor] released %d stale 'executing' claim(s)", n)
    return n


def run_executable_findings(*, limit: int = 5) -> Dict[str, Any]:
    """Pick up auto-apply investigated findings whose 24h grace has expired
    and execute them sequentially. Wired to the kg_finding_executor_drain
    routine (03:45 daily)."""
    _reclaim_stale_executions()
    ids = _claim_executable_finding_ids(limit=limit)
    if not ids:
        return {"status": "no_executable_findings", "processed": 0, "results": []}

    # execute_one() returns one of: "executed", "escalated", "error",
    # "skipped". Bucket these explicitly so the log reflects reality
    # instead of marking every escalation as an error.
    results: List[Dict[str, Any]] = []
    executed = escalated = errors = skipped = 0
    for fid in ids:
        r = execute_one(fid)
        results.append(r)
        st = r.get("status")
        if st == "executed":
            executed += 1
        elif st == "escalated":
            escalated += 1
        elif st == "error":
            errors += 1
        else:
            skipped += 1

    logger.info(
        "[finding_executor] processed=%d executed=%d escalated=%d errors=%d skipped=%d",
        len(ids), executed, escalated, errors, skipped,
    )
    return {
        "status": "ok",
        "processed": len(ids),
        "executed": executed,
        "escalated": escalated,
        "errors": errors,
        "skipped": skipped,
        "results": results,
    }
