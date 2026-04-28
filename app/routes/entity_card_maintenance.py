"""
/entity-card-maintenance — Entity card maintenance findings review UI.

GET  /entity-card-maintenance              → dashboard HTML
GET  /entity-card-maintenance/api/findings → JSON list (filterable)
GET  /entity-card-maintenance/api/summary  → JSON counts by type / status
POST /entity-card-maintenance/api/action   → approve / reject / snooze a finding
POST /entity-card-maintenance/api/bulk_action → bulk status update
POST /entity-card-maintenance/api/run      → trigger an on-demand pipeline run

Store functions manage their own sessions; this route has no direct DB access.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from app.assistant.entity_card_maintenance.store import (
    execute_approved_finding,
    get_finding,
    get_findings,
    get_summary_counts,
    set_status,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

entity_card_maintenance_bp = Blueprint(
    "entity_card_maintenance", __name__, url_prefix="/entity-card-maintenance"
)


@entity_card_maintenance_bp.route("/", methods=["GET"])
def dashboard():
    try:
        summary = get_summary_counts()
        return render_template("entity_card_maintenance.html", summary=summary)
    except Exception:
        logger.debug("[entity_card_maintenance] dashboard failed", exc_info=True)
        raise


@entity_card_maintenance_bp.route("/api/findings", methods=["GET"])
def api_findings():
    status       = request.args.get("status", "pending")
    finding_type = request.args.get("type") or None
    priority     = request.args.get("priority") or None
    try:
        limit  = min(int(request.args.get("limit",  200)), 500)
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "limit and offset must be integers"}), 400
    try:
        findings = get_findings(
            status=status,
            finding_type=finding_type,
            priority=priority,
            limit=limit,
            offset=offset,
        )
        return jsonify(findings)
    except Exception:
        logger.debug("[entity_card_maintenance] api_findings failed", exc_info=True)
        raise


@entity_card_maintenance_bp.route("/api/summary", methods=["GET"])
def api_summary():
    try:
        return jsonify(get_summary_counts())
    except Exception:
        logger.debug("[entity_card_maintenance] api_summary failed", exc_info=True)
        raise


@entity_card_maintenance_bp.route("/api/action", methods=["POST"])
def api_action():
    """Body: { "id": "<finding_id>", "action": "approve"|"reject"|"snooze"|"regenerate" }

    ``approve`` runs the finding's default suggested_action (deactivate or review).
    ``regenerate`` overrides to a v2 card regeneration regardless of the default.
    ``reject`` and ``snooze`` set status only.
    """
    data   = request.get_json(force=True) or {}
    fid    = data.get("id")
    action = (data.get("action") or "").strip().lower()

    if not fid:
        return jsonify({"error": "id is required"}), 400

    action_to_status = {
        "approve":    "approved",
        "regenerate": "approved",
        "reject":     "rejected",
        "snooze":     "snoozed",
    }
    new_status = action_to_status.get(action)
    if not new_status:
        return jsonify({"error": f"Unknown action {action!r}"}), 400

    try:
        finding = get_finding(fid)
        if finding is None:
            return jsonify({"error": "Finding not found"}), 404
        if action in ("approve", "regenerate"):
            set_status(fid, new_status, executed_by="ui")
            override = "regenerate" if action == "regenerate" else None
            exec_result = execute_approved_finding(fid, override_action=override)
            result = get_finding(fid)
            result["exec_detail"] = exec_result.get("detail", "")
            return jsonify(result)
        set_status(fid, new_status, executed_by="ui")
        return jsonify(get_finding(fid))
    except Exception:
        logger.debug("[entity_card_maintenance] api_action failed id=%s", fid, exc_info=True)
        raise


@entity_card_maintenance_bp.route("/api/bulk_action", methods=["POST"])
def api_bulk_action():
    """Body: { "ids": ["<id>", ...], "action": "approve"|"reject"|"snooze"|"regenerate" }"""
    data   = request.get_json(force=True) or {}
    ids    = data.get("ids") or []
    action = (data.get("action") or "").strip().lower()

    action_to_status = {
        "approve":    "approved",
        "regenerate": "approved",
        "reject":     "rejected",
        "snooze":     "snoozed",
    }
    new_status = action_to_status.get(action)
    if not new_status:
        return jsonify({"error": f"Unknown action {action!r}"}), 400
    if not ids:
        return jsonify({"error": "ids list is required"}), 400

    try:
        updated = 0
        executed = 0
        override = "regenerate" if action == "regenerate" else None
        for fid in ids:
            f = get_finding(fid)
            if f and f["status"] == "pending":
                set_status(fid, new_status, executed_by="ui_bulk")
                updated += 1
                if action in ("approve", "regenerate"):
                    result = execute_approved_finding(fid, override_action=override)
                    if result.get("executed"):
                        executed += 1
        return jsonify({"updated": updated, "executed": executed})
    except Exception:
        logger.debug("[entity_card_maintenance] bulk_action failed", exc_info=True)
        raise


@entity_card_maintenance_bp.route("/api/run", methods=["POST"])
def api_run():
    """Trigger an on-demand pipeline run."""
    try:
        from app.assistant.pipelines.entity_card_maintenance_pipeline.pipeline import (
            EntityCardMaintenancePipeline,
        )
        result = EntityCardMaintenancePipeline().run()
        return jsonify(result)
    except Exception:
        logger.debug("[entity_card_maintenance] api_run failed", exc_info=True)
        raise
