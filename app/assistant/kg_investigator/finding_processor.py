"""Loop pending kg_maintenance_finding rows through kg_investigation_manager.

For each pending finding:
  1. Build an investigator brief (finding_brief.build_finding_brief).
  2. Invoke kg_investigation_manager with the brief.
  3. Pull the structured report from the final-answer agent's audit message.
  4. Persist the report into kg_maintenance_finding.investigation_report_json
     and bump status to 'investigated'.

Idempotent: skips findings whose status is not 'pending'.

A failed manager run (an exception, or a run ManagerInvoker returns as
``manager_aborted``) stops the queue: the finding stays 'pending' for the
next run and the failure is counted in the KG failure ledger, which sets
aside a finding that fails in three runs (owner, 2026-10-05).

Wireable to a routine: ``run_pending_findings(limit=N)`` returns a small
result dict the routine can write to its day file.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.kg import failure_ledger
from app.assistant.kg_investigator.finding_brief import build_finding_brief
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.identity_names import PRINCIPAL_USER
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeContext, ToolResult
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

MANAGER_NAME = "kg_investigation_manager"
MUTATION_MANAGER_NAME = "kg_mutation_manager"
LEDGER_KIND = "kg_finding_investigation"


class ManagerAborted(RuntimeError):
    """A manager run ended aborted. ManagerInvoker returns an abort as a
    ``manager_aborted`` result rather than raising, so callers check for it."""


def invoke_manager(manager_name: str, mgr, msg: Message) -> ToolResult:
    """Run one manager invocation; raise when it fails or aborts."""
    if mgr is None:
        raise RuntimeError(f"manager {manager_name} not registered")
    result = DI.manager_invoker.invoke(mgr, msg)
    if result.result_type == "manager_aborted":
        raise ManagerAborted(f"{manager_name} aborted: {result.content}")
    return result


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
    return load_scope_for_source(
        kind="subsystem",
        source_id="kg_investigator",
        actor_id="kg_finding_investigator",
        identity_overrides={
            "owner_id": PRINCIPAL_USER,
            "actor_id": "kg_finding_investigator",
            "scope_id": "scope::kg_investigator::finding_processor",
        },
    )


def finding_write_scope() -> ScopeContext:
    """Scope context for the KG-writing side of finding resolution.

    Shared by the kg_mutation_manager (applies a previously-recorded
    proposed_action via narrow audit-wired tools) and the resolution
    manager invoked from finding_executor (full read + mutate suite;
    safety lives in the dev-page 24h grace + Accept review, not in tool
    exclusion). write_kg is True in both cases.
    """
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
    "not_found", "finding_id": ..., "result_summary": ..., ...}``; a failed
    or aborted manager run raises. The terminal finding-row status
    (executed / rejected / escalated) is written by the planner via
    ``kg_finding_resolve`` / ``kg_finding_escalate`` — we don't touch it
    directly here.
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
    msg = Message(task=task, information=information, scope_context=finding_write_scope())
    invoke_manager(MUTATION_MANAGER_NAME, mgr, msg)

    # Read back the finding's terminal state so the caller can see what the
    # planner actually did (executed / rejected / escalated / unchanged).
    with get_db_manager().read_session() as session:
        f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
        terminal_status = f.status if f else None
        execution_notes = f.execution_notes if f else None

    # Pull a result summary from the mutation_manager's final_answer if there is one.
    result_summary: Optional[str] = None
    for m in mgr.blackboard.get_messages():
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

    return {
        "status": "applied",
        "finding_id": finding_id,
        "terminal_status": terminal_status,
        "execution_notes": execution_notes,
        "result_summary": result_summary,
    }


def _claim_pending_finding_ids(
    *,
    limit: int,
    finding_types: Optional[List[str]] = None,
    exclude_finding_types: Optional[List[str]] = None,
) -> List[str]:
    """Pick up to `limit` pending findings, ordered by max importance of
    touched nodes (high first), date as tie-breaker. Optionally restricted
    to specific finding types (`finding_types`) or with some types excluded
    (`exclude_finding_types`, e.g. ['duplicate_node'] which are resolved by
    the dedicated kg_dup_cluster_drain cluster path, not the per-pair drain).
    Findings the failure ledger set aside are skipped."""
    from app.assistant.kg_investigator.finding_priority import order_by_importance

    set_aside = failure_ledger.set_aside_ids(LEDGER_KIND)
    with get_db_manager().read_session() as session:
        q = (
            session.query(
                KGMaintenanceFinding.id,
                KGMaintenanceFinding.primary_node_id,
                KGMaintenanceFinding.created_at,
            )
            .filter(KGMaintenanceFinding.status == "pending")
            # Superseded siblings are resolved by their cluster lead's
            # cascade — investigating them independently duplicates LLM
            # spend and can produce conflicting verdicts.
            .filter(KGMaintenanceFinding.superseded_by.is_(None))
        )
        if finding_types:
            q = q.filter(KGMaintenanceFinding.finding_type.in_(finding_types))
        if exclude_finding_types:
            q = q.filter(~KGMaintenanceFinding.finding_type.in_(exclude_finding_types))
        rows = q.all()

    candidates = [(r[0], r[1], r[2]) for r in rows if r[0] not in set_aside]
    claimed = order_by_importance(candidates)[:limit]

    # Fairness (audit P3.1): importance-DESC alone starves low-importance
    # findings forever. Reserve one slot for the oldest pending candidate.
    if claimed and len(candidates) > len(claimed):
        oldest_fid = min(
            candidates, key=lambda c: (c[2] is None, c[2] or "")
        )[0]
        if oldest_fid not in claimed:
            claimed[-1] = oldest_fid
    return claimed


def _extract_report_from_audit(blackboard) -> Optional[Dict[str, Any]]:
    """Pull the structured report from the kg_investigation::final_answer audit message."""
    for m in blackboard.get_messages():
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


def _node_drift_snapshot(session, node_ids) -> Dict[str, Any]:
    """Compact per-node snapshot persisted with the report so the executor
    can detect drift before applying a ≥24h-old recommendation (audit
    P0.4). Reads via raw SQL + str() so the executor's comparison (same
    read) is byte-for-byte comparable. updated_at is captured for
    telemetry only — it moves on every re-observation stamp, so the
    executor does NOT gate on it."""
    from sqlalchemy import text as sql_text

    snap: Dict[str, Any] = {}
    for nid in node_ids:
        if not nid:
            continue
        row = session.execute(
            sql_text(
                "SELECT label, start_date, end_date, updated_at "
                "FROM kg_node_metadata WHERE id = :nid"
            ),
            {"nid": nid},
        ).fetchone()
        if row is None:
            continue
        snap[nid] = {
            "label": row[0],
            "start_date": str(row[1]) if row[1] is not None else None,
            "end_date": str(row[2]) if row[2] is not None else None,
            "updated_at": str(row[3]) if row[3] is not None else None,
        }
    return snap


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
        report = dict(report)
        report["node_snapshot_at_investigation"] = _node_drift_snapshot(
            session, (f.primary_node_id, f.secondary_node_id)
        )
        f.investigation_report_json = report
        f.investigated_at = datetime.now(timezone.utc)
        f.status = "investigated"


def investigate_one(finding_id: str) -> Dict[str, Any]:
    """Investigate a single finding by id. Returns a small result dict.

    A failed or aborted manager run, or a run that ends without a structured
    report, raises and leaves the finding 'pending': an outage is never
    recorded as a verdict or counted toward escalation."""
    brief = build_finding_brief(finding_id)
    if brief is None:
        return {"status": "not_found", "finding_id": finding_id}
    task, information = brief

    mgr = DI.multi_agent_manager_factory.create_manager(MANAGER_NAME)
    msg = Message(task=task, information=information, scope_context=_investigation_scope())
    invoke_manager(MANAGER_NAME, mgr, msg)

    report = _extract_report_from_audit(mgr.blackboard)
    if report is None:
        raise RuntimeError(
            f"{MANAGER_NAME} finished without a structured report for finding {finding_id}"
        )

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
        # Use the finding's own subject node ids ONLY when the
        # investigator gave us a verdict_type + memo but forgot the ids.
        # Don't synthesize verdicts when the investigator skipped the
        # whole contract — that's an investigator bug we want surfaced,
        # not papered over.
        # PAIR verdicts only for pairwise finding types (audit P3.5): a
        # single-node finding can carry an incidental secondary id, and
        # minting a (primary, secondary) pair verdict from it would be an
        # assertion the investigator never made.
        _PAIRWISE_FINDING_TYPES = {"duplicate_node", "duplicate_edge"}
        if not verdict_node_ids and verdict_type and memo:
            with get_db_manager().read_session() as session:
                f = session.query(KGMaintenanceFinding).filter_by(id=finding_id).first()
                if f is not None:
                    if (f.finding_type or "") in _PAIRWISE_FINDING_TYPES:
                        verdict_node_ids = [
                            nid for nid in (f.primary_node_id, f.secondary_node_id) if nid
                        ]
                    else:
                        verdict_node_ids = [f.primary_node_id] if f.primary_node_id else []

        # Hallucinated ids must not become durable "prior verdicts" that
        # future agents consult (audit P1.4). Validate liveness before
        # persisting; dead ids → escalate like any other contract breach.
        if verdict_node_ids:
            from sqlalchemy import text as sql_text
            with get_db_manager().read_session() as session:
                placeholders = ", ".join(
                    f":id{i}" for i in range(len(verdict_node_ids))
                )
                live = {
                    str(r[0]) for r in session.execute(
                        sql_text(
                            f"SELECT id FROM kg_node_metadata "
                            f"WHERE id IN ({placeholders})"
                        ),
                        {f"id{i}": nid for i, nid in enumerate(verdict_node_ids)},
                    )
                }
            dead_ids = [nid for nid in verdict_node_ids if nid not in live]
            if dead_ids:
                logger.error(
                    "[finding_processor] verdict_node_ids contain non-live "
                    "node id(s) %s on finding %s — escalating instead of "
                    "recording a verdict about nothing",
                    dead_ids, finding_id,
                )
                set_status(
                    finding_id,
                    "escalated",
                    executed_by="agent:kg_investigation",
                    execution_notes=(
                        f"Investigator's verdict_node_ids include id(s) not "
                        f"present in kg_node_metadata: {dead_ids}. No verdict "
                        f"recorded; escalated for human review."
                    ),
                )
                return {
                    "status": "escalated",
                    "finding_id": finding_id,
                    "reason": "verdict_node_ids_not_live",
                }

        if not (verdict_type and memo and verdict_node_ids):
            # Investigator violated the take_action=False contract. Escalate
            # rather than dismiss — silently dropping a finding with no
            # verdict means future runs re-investigate the same question
            # forever and we don't notice the broken contract.
            logger.error(
                "[finding_processor] take_action=False but contract violated "
                "(verdict_type=%r memo=%r ids=%r) on finding %s — escalating",
                verdict_type, memo, verdict_node_ids, finding_id,
            )
            set_status(
                finding_id,
                "escalated",
                executed_by="agent:kg_investigation",
                execution_notes=(
                    "Investigator emitted take_action=False but missing one or "
                    "more of verdict_type / verdict_memo / verdict_node_ids. "
                    "No verdict recorded; finding escalated for human review."
                ),
            )
            return {
                "status": "escalated",
                "finding_id": finding_id,
                "reason": "take_action_false_contract_violation",
            }

        verdict_id = record_verdict(
            verdict_type=verdict_type,
            memo=memo,
            node_ids=verdict_node_ids,
            decided_by="agent:kg_investigation",
            reasoning=report.get("recommendation"),
            source_finding_id=finding_id,
            confidence=report.get("confidence"),
        )
        if verdict_id is None:
            # record_verdict returned None — input rejected (unknown
            # verdict_type, etc.). Surface as escalation, not dismiss.
            logger.error(
                "[finding_processor] verdict write rejected on finding %s "
                "— escalating instead of dismissing",
                finding_id,
            )
            set_status(
                finding_id,
                "escalated",
                executed_by="agent:kg_investigation",
                execution_notes=(
                    f"Investigator emitted take_action=False with "
                    f"verdict_type={verdict_type!r} but verdict_store "
                    f"rejected the write. No verdict recorded; escalated."
                ),
            )
            return {
                "status": "escalated",
                "finding_id": finding_id,
                "reason": "verdict_write_rejected",
            }

        set_status(
            finding_id,
            "dismissed",
            executed_by="agent:kg_investigation",
            execution_notes=(
                f"Investigator verdict: no action needed. "
                f"Memo: {memo} (verdict_id={verdict_id})"
            ),
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
    exclude_finding_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Drain up to `limit` pending findings from the backlog, ordered by
    importance of the touched nodes (high first; one slot reserved for
    the oldest pending row so low-importance findings can't starve).

    Routine entry-point — different from the pipeline's
    ``_investigate_findings_for_run`` which only sees that run's own findings.
    This function picks from the global pending queue regardless of which
    pipeline run produced them, so old findings don't sit forever.

    Optional ``finding_types`` restricts to specific types
    (e.g. ['orphan_node']); ``exclude_finding_types`` drops types handled
    elsewhere (e.g. ['duplicate_node'], which kg_dup_cluster_drain resolves
    at cluster scale so the per-pair investigator never touches them).
    """
    ids = _claim_pending_finding_ids(
        limit=limit,
        finding_types=finding_types,
        exclude_finding_types=exclude_finding_types,
    )
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

    Skips ids that aren't pending (e.g., already investigated or rejected)
    and ids the failure ledger set aside. Caps total work at
    ``max_to_investigate`` to keep producer routines bounded. The first
    finding whose investigation fails stops the run: its failure is counted
    in the ledger and the exception propagates; the rest stay pending.
    """
    ids = list(dict.fromkeys((fid or "").strip() for fid in (finding_ids or []) if fid))
    if not ids:
        return {"status": "no_ids", "processed": 0, "results": []}

    # Filter to ids that are still pending — others (already investigated /
    # rejected / executed) shouldn't be redone.
    set_aside = failure_ledger.set_aside_ids(LEDGER_KIND)
    with get_db_manager().read_session() as session:
        rows = (
            session.query(
                KGMaintenanceFinding.id,
                KGMaintenanceFinding.finding_type,
                KGMaintenanceFinding.primary_node_id,
            )
            .filter(KGMaintenanceFinding.id.in_(ids))
            .filter(KGMaintenanceFinding.status == "pending")
            .all()
        )
        labels = {
            r[0]: f"{r[1]} finding on node {r[2]}" for r in rows if r[0] not in set_aside
        }
    pending_ids = [fid for fid in ids if fid in labels]

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
    for fid in pending_ids:
        try:
            r = investigate_one(fid)
        except Exception as exc:
            failure_ledger.record_failure(LEDGER_KIND, fid, exc, label=labels[fid])
            raise
        failure_ledger.clear(LEDGER_KIND, fid)
        results.append(r)
        if r.get("status") == "investigated":
            investigated += 1

    logger.info(
        "[finding_processor] processed=%d investigated=%d (from %d ids)",
        len(pending_ids), investigated, len(ids),
    )
    return {
        "status": "ok",
        "processed": len(pending_ids),
        "investigated": investigated,
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
    return investigate_findings(ids, max_to_investigate=limit)
