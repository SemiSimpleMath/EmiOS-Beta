"""
/kg-maintenance — Unified KG maintenance findings review UI.

GET  /kg-maintenance                    → dashboard HTML
GET  /kg-maintenance/api/findings       → JSON list (filterable, enriched with node labels)
GET  /kg-maintenance/api/summary        → JSON counts by type / status
POST /kg-maintenance/api/action         → approve / reject / execute a finding
POST /kg-maintenance/api/bulk_action    → bulk status update
POST /kg-maintenance/api/execute_approved → execute all approved findings
POST /kg-maintenance/api/run            → trigger a scan (never auto-executes)

Store functions manage their own sessions; this route has no direct DB access.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

from app.assistant.kg_maintenance.store import (
    execute_finding,
    get_finding,
    get_findings,
    get_summary_counts,
    set_status,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

kg_maintenance_bp = Blueprint("kg_maintenance", __name__, url_prefix="/kg-maintenance")


@kg_maintenance_bp.route("/", methods=["GET"])
def dashboard():
    try:
        summary = get_summary_counts(exclude_types=_DASHBOARD_HIDDEN_TYPES)
        return render_template("kg_maintenance.html", summary=summary)
    except Exception:
        logger.debug("[kg_maintenance] dashboard failed", exc_info=True)
        raise


# Set of finding types (and their suggested_actions) that the auto-executor
# knows how to run. Findings outside this set are surfaced as "needs human
# review" in the action queue.
_AUTO_EXECUTABLE_TYPES: frozenset[str] = frozenset({"duplicate_node", "orphan_node"})

# Finding types that have their own dedicated UI page and should be hidden
# from the default main-dashboard list. Each of these is a long-running
# background queue (date-gap questions accumulate in the hundreds) and would
# drown out the actionable triage findings if mixed in.
_DASHBOARD_HIDDEN_TYPES: frozenset[str] = frozenset({"state_missing_dates"})


@kg_maintenance_bp.route("/date-gaps", methods=["GET"])
def date_gaps():
    """Dedicated page for state_missing_dates findings — the user-facing
    queue of bounded-category States/Events with no start_date. Hidden from
    the main triage dashboard so the question backlog doesn't drown the
    actionable findings."""
    try:
        return render_template("kg_maintenance_date_gaps.html")
    except Exception:
        logger.debug("[kg_maintenance] date_gaps failed", exc_info=True)
        raise


@kg_maintenance_bp.route("/queue", methods=["GET"])
def action_queue():
    """Dedicated view of approved findings (user-flagged work queue).

    Split into two sections: auto-executable (pipeline can resolve) vs.
    needs-human-review (requires manual action). The triage page at `/` is
    where findings first appear as ``pending``; this page is where they
    live after the user has flagged them for action.
    """
    try:
        summary = get_summary_counts(exclude_types=_DASHBOARD_HIDDEN_TYPES)
        return render_template(
            "kg_maintenance_queue.html",
            summary=summary,
            auto_types=sorted(_AUTO_EXECUTABLE_TYPES),
        )
    except Exception:
        logger.debug("[kg_maintenance] action_queue failed", exc_info=True)
        raise


@kg_maintenance_bp.route("/api/findings", methods=["GET"])
def api_findings():
    status = request.args.get("status", "pending")
    finding_type = request.args.get("type") or None
    priority = request.args.get("priority") or None
    try:
        limit = min(int(request.args.get("limit", 200)), 500)
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "limit and offset must be integers"}), 400
    # Hide background-queue types from the main dashboard unless the caller
    # explicitly asks for one of them via ?type=.
    exclude_types = (
        list(_DASHBOARD_HIDDEN_TYPES)
        if not finding_type
        else None
    )
    try:
        findings = get_findings(
            status=status,
            finding_type=finding_type,
            priority=priority,
            exclude_types=exclude_types,
            limit=limit,
            offset=offset,
        )
        return jsonify(findings)
    except Exception:
        logger.debug("[kg_maintenance] api_findings failed", exc_info=True)
        raise


@kg_maintenance_bp.route("/api/summary", methods=["GET"])
def api_summary():
    try:
        return jsonify(get_summary_counts())
    except Exception:
        logger.debug("[kg_maintenance] api_summary failed", exc_info=True)
        raise


@kg_maintenance_bp.route("/api/action", methods=["POST"])
def api_action():
    """Body: { "id": "<finding_id>", "action": "approve"|"reject"|"execute" }"""
    data = request.get_json(force=True) or {}
    finding_id = data.get("id")
    action = (data.get("action") or "").strip().lower()

    if not finding_id:
        return jsonify({"error": "id is required"}), 400

    if action == "execute":
        try:
            finding = get_finding(finding_id)
            if finding is None:
                return jsonify({"error": "Finding not found"}), 404
            result = execute_finding(finding_id)
            updated = get_finding(finding_id)
            if updated:
                updated["execute_result"] = result
            return jsonify(updated or result)
        except Exception:
            logger.debug("[kg_maintenance] execute failed id=%s", finding_id, exc_info=True)
            raise

    action_to_status = {
        "approve": "approved",
        "reject": "rejected",
    }
    new_status = action_to_status.get(action)
    if not new_status:
        return jsonify({"error": f"Unknown action '{action}'"}), 400

    try:
        finding = get_finding(finding_id)
        if finding is None:
            return jsonify({"error": "Finding not found"}), 404
        set_status(finding_id, new_status, executed_by="ui")
        return jsonify(get_finding(finding_id))
    except Exception:
        logger.debug("[kg_maintenance] api_action failed id=%s", finding_id, exc_info=True)
        raise


@kg_maintenance_bp.route("/api/finding/<finding_id>/fill_dates", methods=["POST"])
def api_fill_dates(finding_id):
    """Manual resolver for a state_missing_dates finding.

    Body fields (all optional, but at least one date or prose must be set):
      start_date           : YYYY-MM-DD or ISO datetime, or "" to clear
      end_date             : same shape
      start_date_prose     : free-text approximation ("around 2010", "early 2020s")
      end_date_prose       : same
      start_date_confidence: 'high' | 'medium' | 'low' (optional)
      end_date_confidence  : same

    Writes the updates onto kg_node_metadata for the finding's
    primary_node_id, then marks the finding executed (status='executed')
    so it falls off the date-gaps queue. Logs a kg_revision_log row
    for audit.
    """
    from sqlalchemy import text as sql_text
    from app.assistant.kg_maintenance.store import get_finding, set_status
    from app.models.base import get_session as _get_session

    data = request.get_json(force=True) or {}
    finding = get_finding(finding_id)
    if finding is None:
        return jsonify({"error": "Finding not found"}), 404
    if finding.get("finding_type") != "state_missing_dates":
        return jsonify({"error": "fill_dates only valid for state_missing_dates findings"}), 400

    node_id = finding.get("primary_node_id")
    if not node_id:
        return jsonify({"error": "Finding has no primary_node_id"}), 400

    # Collect updateable fields. None means "leave alone"; empty string means
    # "clear the column to NULL".
    fields = {}
    for k in ("start_date", "end_date", "start_date_prose", "end_date_prose",
              "start_date_confidence", "end_date_confidence"):
        if k in data:
            v = data.get(k)
            if v is None or (isinstance(v, str) and v.strip() == ""):
                fields[k] = None
            else:
                fields[k] = str(v).strip()

    if not fields:
        return jsonify({"error": "no fields to update"}), 400

    session = _get_session()
    try:
        # Capture before-state of date columns for the audit log
        before_row = session.execute(
            sql_text(
                "SELECT start_date, end_date, start_date_prose, end_date_prose, "
                "       start_date_confidence, end_date_confidence "
                "FROM kg_node_metadata WHERE id = :nid"
            ),
            {"nid": node_id},
        ).first()
        if before_row is None:
            session.rollback()
            return jsonify({"error": f"node {node_id} not found"}), 404
        before_state = {
            "start_date": before_row[0], "end_date": before_row[1],
            "start_date_prose": before_row[2], "end_date_prose": before_row[3],
            "start_date_confidence": before_row[4], "end_date_confidence": before_row[5],
        }

        # Apply node updates
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        params = dict(fields)
        params["nid"] = node_id
        params["now"] = datetime.utcnow().isoformat()
        session.execute(
            sql_text(f"UPDATE kg_node_metadata SET {sets}, updated_at = :now WHERE id = :nid"),
            params,
        )

        # Audit row in kg_revision_log (schema: op, args_json, before_json,
        # after_json, reason, finding_id, agent_id, succeeded, ...)
        after_state = dict(before_state)
        after_state.update(fields)
        try:
            session.execute(
                sql_text(
                    "INSERT INTO kg_revision_log "
                    "(id, op, args_json, before_json, after_json, reason, "
                    " finding_id, agent_id, succeeded, created_at) "
                    "VALUES (:id, :op, :args, :before, :after, :reason, "
                    "        :fid, :agent, 1, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "op": "fill_dates_manual",
                    "args": json.dumps({"node_id": node_id, "patch": fields}),
                    "before": json.dumps(before_state, default=str),
                    "after": json.dumps(after_state, default=str),
                    "reason": "manual fill via /kg-maintenance/date-gaps",
                    "fid": finding_id,
                    "agent": "ui:fill_dates",
                    "now": datetime.utcnow().isoformat(),
                },
            )
        except Exception:
            logger.debug("[fill_dates] revision-log insert skipped", exc_info=True)

        session.commit()
    finally:
        session.close()

    # Mark finding executed
    try:
        set_status(
            finding_id,
            "executed",
            executed_by="ui:fill_dates",
            execution_notes=f"manual fill: {sorted(fields.keys())}",
        )
    except Exception:
        # Older signatures of set_status don't accept execution_notes.
        try:
            set_status(finding_id, "executed", executed_by="ui:fill_dates")
        except Exception:
            logger.debug("[fill_dates] set_status failed", exc_info=True)

    return jsonify({"ok": True, "node_id": node_id, "applied": fields})


@kg_maintenance_bp.route("/api/finding/<finding_id>/investigate", methods=["POST"])
def api_investigate_finding(finding_id):
    """
    Investigate a single pending finding on demand. Synchronous: invokes the
    kg_investigation_manager and waits for the structured report. Returns
    the small result dict {status, finding_id, proposed_op, summary}.

    Status transitions: pending -> investigated (on success).
    """
    try:
        from app.assistant.kg_investigator.finding_processor import investigate_one
        result = investigate_one(finding_id)
        return jsonify(result)
    except Exception as e:
        logger.error("[kg_maintenance] investigate failed id=%s: %s", finding_id, e, exc_info=True)
        return jsonify({"error": str(e)}), 500


@kg_maintenance_bp.route("/api/bulk_action", methods=["POST"])
def api_bulk_action():
    """Body: { "ids": ["<id>", ...], "action": "approve"|"reject" }"""
    data = request.get_json(force=True) or {}
    ids = data.get("ids") or []
    action = (data.get("action") or "").strip().lower()

    action_to_status = {
        "approve": "approved",
        "reject": "rejected",
    }
    new_status = action_to_status.get(action)
    if not new_status:
        return jsonify({"error": f"Unknown action '{action}'"}), 400
    if not ids:
        return jsonify({"error": "ids list is required"}), 400

    try:
        updated = 0
        for finding_id in ids:
            f = get_finding(finding_id)
            if f and f["status"] == "pending":
                set_status(finding_id, new_status, executed_by="ui_bulk")
                updated += 1
        return jsonify({"updated": updated})
    except Exception:
        logger.debug("[kg_maintenance] bulk_action failed", exc_info=True)
        raise


@kg_maintenance_bp.route("/api/execute_approved", methods=["POST"])
def api_execute_approved():
    """Execute all findings currently in 'approved' status."""
    try:
        from app.assistant.pipelines.kg_maintenance_pipeline.step_execute_findings import (
            execute_single_finding,
        )

        approved = get_findings(status="approved", limit=500)
        executed = 0
        errors = 0
        results: list[dict] = []

        for finding in approved:
            fid = finding["id"]
            try:
                result = execute_single_finding(finding)
                if result.get("executed"):
                    set_status(fid, "executed", executed_by="ui_batch", execution_notes=result.get("detail", ""))
                    executed += 1
                else:
                    set_status(fid, "rejected", executed_by="ui_batch", execution_notes=result.get("detail", ""))
                results.append({"id": fid, **result})
            except Exception as exc:
                logger.error("[kg_maintenance] execute_approved failed id=%s: %s", fid, exc)
                logger.debug("[kg_maintenance] exception details", exc_info=True)
                set_status(fid, "execute_error", executed_by="ui_batch", execution_notes=str(exc)[:5000])
                errors += 1
                results.append({"id": fid, "executed": False, "detail": str(exc)[:2000]})

        return jsonify({"executed": executed, "errors": errors, "details": results})
    except Exception:
        logger.debug("[kg_maintenance] execute_approved failed", exc_info=True)
        raise


@kg_maintenance_bp.route("/api/run", methods=["POST"])
def api_run():
    """
    Trigger an on-demand scan.  Never auto-executes findings — scan only.

    Body (all optional):
      skip_steps          — step names to skip (on top of execute_findings which
                            is always skipped from the UI).
                            Defaults to skipping LLM-heavy steps for a fast
                            structural-only scan.
      description_max_nodes — cap for description_fill step (default 50).
    """
    data = request.get_json(force=True) or {}
    skip_steps = data.get(
        "skip_steps",
        ["duplicate_scan", "description_fill"],
    )
    if "execute_findings" not in skip_steps:
        skip_steps.append("execute_findings")
    description_max_nodes = int(data.get("description_max_nodes", 50))
    try:
        from app.assistant.pipelines.kg_maintenance_pipeline.pipeline import KGMaintenancePipeline
        result = KGMaintenancePipeline().run(
            skip_steps=skip_steps,
            description_max_nodes=description_max_nodes,
        )
        return jsonify(result)
    except Exception:
        logger.debug("[kg_maintenance] api_run failed", exc_info=True)
        raise
