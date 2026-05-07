from __future__ import annotations

# All dayflow orchestrator logic has moved to:
#   app.assistant.dayflow_orchestrator.dayflow_tick
#
# This module now only owns the cron routine dispatch registry.
# The re-exports below keep existing importers working without changes.

from app.assistant.dayflow_orchestrator.orchestrator_status import (  # noqa: F401
    block_dayflow_orchestrator_for_master_chat,
    DAYFLOW_ORCHESTRATOR_ROOM_ID,
    DAYFLOW_ORCHESTRATOR_STATUS_RESOURCE_ID,
    MASTER_ROOM_BLOCK_SECONDS,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _lazy_cadence_tick(*, target_date=None, routine=None):
    """Lazy import to avoid circular import at module load time."""
    from app.assistant.dayflow_orchestrator.dayflow_tick import dayflow_orchestrator_cadence_tick
    return dayflow_orchestrator_cadence_tick(target_date=target_date, routine=routine)


dayflow_orchestrator_cadence_tick = _lazy_cadence_tick

def _lazy_situation_audit(*, target_date=None, routine=None):
    """Run the situation auditor — periodic background audit of all active context."""
    from app.assistant.pipelines.dayflow.utils.situation_audit_runner import run_situation_audit
    return run_situation_audit()


situation_audit = _lazy_situation_audit


def _lazy_location_refresh(*, target_date=None, routine=None):
    """Refresh the location manager — rebuild timeline and update resource_current_location.json."""
    from app.assistant.location_manager import get_location_manager
    get_location_manager().refresh()


location_refresh = _lazy_location_refresh


def _lazy_chat_memory_index(*, target_date=None, routine=None):
    """Index recent chat summaries into ChromaDB for conversational memory."""
    from app.assistant.agent_runtime.services.chat_memory_rag import index_recent_summaries
    index_recent_summaries()


chat_memory_index = _lazy_chat_memory_index


def _lazy_proposal_promoter(*, target_date=None, routine=None):
    """Nightly: walk pending claim_proposals and apply promotions."""
    from app.assistant.kg.proposal_promoter import run_promoter
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    commit = bool(spec.get("commit", False))
    limit = int(spec.get("limit", 500))
    return run_promoter(limit=limit, commit=commit)


proposal_promoter = _lazy_proposal_promoter


def _lazy_kg_finding_backlog_drain(*, target_date=None, routine=None):
    """Daily: investigate the oldest N pending kg_maintenance findings (FIFO).

    The weekly kg_maintenance_pipeline only investigates findings produced by
    its own run, leaving older pending findings to accumulate. This drain
    routine picks from the global pending queue so the backlog doesn't grow.
    """
    from app.assistant.kg_investigator.finding_processor import drain_pending_findings
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    limit = int(spec.get("limit", 5))
    finding_types = spec.get("finding_types") or None
    return drain_pending_findings(limit=limit, finding_types=finding_types)


kg_finding_backlog_drain = _lazy_kg_finding_backlog_drain


def _lazy_kg_date_gap_drain(*, target_date=None, routine=None):
    """Daily: pick 1-3 highest-priority state_missing_dates findings to ask
    the user about, marking them as queued so they aren't re-picked
    immediately.

    v1 stops at marking — the actual push-question-to-chat wiring (via
    questioner_manager or a proactive_suggestion ticket) is a follow-up.
    Until that lands, the user-facing surface is /kg-maintenance/date-gaps,
    where the queued items show up first by priority.

    Routine spec:
      limit          — max findings to queue this run (default 3)
      cooldown_days  — re-ask interval; findings asked within this many days
                       are skipped (default 7)
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import case
    from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
    from app.models.db_manager import get_db_manager

    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    limit = max(1, int(spec.get("limit", 3)))
    cooldown_days = max(0, int(spec.get("cooldown_days", 7)))
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)

    queued_ids: list[str] = []
    skipped_in_cooldown = 0
    with get_db_manager().transaction(op="kg_date_gap_drain") as session:
        rows = (
            session.query(KGMaintenanceFinding)
            .filter(KGMaintenanceFinding.finding_type == "state_missing_dates")
            .filter(KGMaintenanceFinding.status == "pending")
            .order_by(
                # high → medium → low
                case(
                    {"high": 1, "medium": 2, "low": 3},
                    value=KGMaintenanceFinding.priority,
                    else_=4,
                ),
                KGMaintenanceFinding.created_at.asc(),
            )
            .limit(limit * 5)  # pull a buffer so cooldown filter has room
            .all()
        )
        for f in rows:
            if len(queued_ids) >= limit:
                break
            ev = dict(f.evidence_json or {})
            last_asked_raw = ev.get("last_asked_at")
            if last_asked_raw:
                try:
                    last_asked = datetime.fromisoformat(last_asked_raw.replace("Z", "+00:00"))
                    if last_asked.tzinfo is None:
                        last_asked = last_asked.replace(tzinfo=timezone.utc)
                    if last_asked > cutoff:
                        skipped_in_cooldown += 1
                        continue
                except ValueError:
                    pass
            ev["last_asked_at"] = datetime.now(timezone.utc).isoformat()
            ev["asked_count"] = int(ev.get("asked_count", 0)) + 1
            f.evidence_json = ev
            queued_ids.append(f.id)

    logger.info(
        "[kg_date_gap_drain] queued=%d skipped_in_cooldown=%d (limit=%d, cooldown_days=%d)",
        len(queued_ids), skipped_in_cooldown, limit, cooldown_days,
    )
    return {
        "queued": len(queued_ids),
        "skipped_in_cooldown": skipped_in_cooldown,
        "queued_finding_ids": queued_ids,
        # TODO: actually push these as questions to the user's chat. Today
        # this routine only marks them as queued so the cooldown works and
        # the /kg-maintenance/date-gaps page sorts queued items first. Real
        # chat delivery via questioner_manager or a proactive_suggestion
        # ticket is the next piece.
    }


kg_date_gap_drain = _lazy_kg_date_gap_drain


def _lazy_kg_state_decay(*, target_date=None, routine=None):
    """Nightly: auto-close State/Event nodes whose TTL has elapsed.

    Runs the same `step_state_decay.run` from the weekly maintenance
    pipeline, but on its own daily schedule. Cheap (no LLM), deterministic
    (uses TTL from `attributes.ttl` set at promotion time + per-node
    `last_observed`), and high-value: keeps the graph from accumulating
    ghost-open states between weekly maintenance runs.
    """
    from app.assistant.pipelines.context import PipelineContext
    from app.assistant.pipelines.kg_maintenance_pipeline.step_state_decay import run as run_state_decay
    ctx = PipelineContext.for_date(pipeline_id="kg_state_decay")
    return run_state_decay(ctx)


kg_state_decay = _lazy_kg_state_decay


def _lazy_kg_finding_cluster_resolve(*, target_date=None, routine=None):
    """Daily: read pending kg_maintenance_findings, group by primary_node_id,
    and call kg_finding_cluster_resolver to verify whether each candidate
    cluster shares ONE root question.

    Confirmed clusters get a lead (with synthesized root_question stored on
    its evidence_json.cluster) plus siblings stamped superseded_by=lead_id.
    The maintenance UI hides supersededs by default — distilling N redundant
    findings into 1 surfaced question. No auto-resolution; the lead still
    goes through the normal review/execute path, but resolving it cascades
    the verdict to its siblings.
    """
    from app.assistant.pipelines.context import PipelineContext
    from app.assistant.pipelines.kg_maintenance_pipeline.step_cluster_resolve import run as run_cluster_resolve
    ctx = PipelineContext.for_date(pipeline_id="kg_finding_cluster_resolve")
    return run_cluster_resolve(ctx)


kg_finding_cluster_resolve = _lazy_kg_finding_cluster_resolve


def _lazy_kg_finding_executor_drain(*, target_date=None, routine=None):
    """Daily: pick the oldest INVESTIGATED findings and let kg_mutation_manager
    apply or escalate. Closes the self-healing loop:
       pending → investigated  (by kg_finding_backlog_drain)
       investigated → executed (by THIS routine)
       OR escalated to user via kg_finding_escalate / a follow-up question

    Limit defaults to 10/day to keep LLM cost bounded; raise if backlog grows.
    """
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    limit = int(spec.get("limit", 10))
    from app.assistant.kg_investigator.finding_executor import run_executable_findings
    return run_executable_findings(limit=limit)


kg_finding_executor_drain = _lazy_kg_finding_executor_drain


def _lazy_kg_state_date_drain(*, target_date=None, routine=None):
    """Daily: drain pending state_missing_dates findings through the
    investigator. Each finding gets the specialized state_missing_dates
    brief (dated neighbors, conversation-evidence window, sibling states),
    and the investigator proposes fill_dates with confidence. Confident
    proposals (>= 0.8) get applied automatically by the next
    kg_finding_executor_drain run; lower-confidence ones surface as
    user-facing questions.
    """
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    limit = int(spec.get("limit", 8))
    from app.assistant.kg_investigator.finding_processor import run_pending_findings
    return run_pending_findings(limit=limit, finding_types=["state_missing_dates"])


kg_state_date_drain = _lazy_kg_state_date_drain


def _lazy_wiki_nightly_refresh(*, target_date=None, routine=None):
    """Nightly: incrementally regenerate wiki pages whose KG neighborhood
    changed since the page was last generated. Optionally runs the
    consistency critic on each refreshed page."""
    from pathlib import Path
    from app.assistant.wiki_generator.nightly_refresh import run_nightly_wiki_refresh
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    vault_raw = spec.get("vault_path") or ""
    vault_path = Path(vault_raw) if vault_raw else None
    run_critic = bool(spec.get("run_critic", True))
    return run_nightly_wiki_refresh(vault_path=vault_path, run_critic=run_critic)


wiki_nightly_refresh = _lazy_wiki_nightly_refresh


def _lazy_kg_importance_rater(*, target_date=None, routine=None):
    """Periodic: rate any node/edge with NULL importance via the existing
    batch raters (me::importance_rater + me::edge_importance_rater). Cheap,
    idempotent, runs every ~30 min so newly-promoted content has scores by
    the time the wiki refresh's importance pre-filter runs against it.

    Reads ``spec.batch_size_edges`` / ``spec.batch_size_nodes`` if you want
    to tune throughput per call.
    """
    from app.me.edge_importance import regenerate_edge_importance
    from app.me.importance import regenerate_importance
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    batch_edges = int(spec.get("batch_size_edges", 50))
    batch_nodes = int(spec.get("batch_size_nodes", 60))

    edge_count = regenerate_edge_importance(batch_size=batch_edges, only_unrated=True)
    node_scores = regenerate_importance(batch_size=batch_nodes, only_unrated=True)
    return {
        "edges_rated": int(edge_count or 0),
        "nodes_rated": len(node_scores or {}),
    }


kg_importance_rater = _lazy_kg_importance_rater


def _lazy_wiki_growth(*, target_date=None, routine=None):
    """Nightly: build up to N new wiki pages for the highest-degree Entity
    nodes that don't yet have one. Pairs with wiki_nightly_refresh —
    refresh maintains existing pages, growth adds new ones over time."""
    from pathlib import Path
    from app.assistant.wiki_generator.growth import run_wiki_growth
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    vault_raw = spec.get("vault_path") or ""
    vault_path = Path(vault_raw) if vault_raw else None
    max_new_pages = int(spec.get("max_new_pages", 5))
    min_degree = int(spec.get("min_degree", 4))
    run_critic = bool(spec.get("run_critic", True))
    return run_wiki_growth(
        vault_path=vault_path,
        max_new_pages=max_new_pages,
        min_degree=min_degree,
        run_critic=run_critic,
    )


wiki_growth = _lazy_wiki_growth


def sleep_camera_tick(*, target_date=None, routine=None):
    """Capture one frame from the bed-facing Ring camera.

    The bridge fires ``ring_snapshot_captured`` after writing the JPEG; the
    sleep_analyzer agent picks it up, finds the previous frame, and emits a
    structured .txt sidecar including motion-vs-previous.

    The camera id comes from the routine spec (``spec.camera_id``) so the
    routine entry in configs/routines.json can be edited without touching
    code if the user re-aims a different camera at the bed.
    """
    spec = (routine.spec if routine and isinstance(getattr(routine, "spec", None), dict) else {}) or {}
    camera_id = str(spec.get("camera_id") or "").strip()
    if not camera_id:
        logger.error("sleep_camera_tick: spec.camera_id is required.")
        return {"status": "error", "error": "missing camera_id in routine spec"}
    try:
        from app.routes import smart_home_bridge as bridge
        result = bridge._ring_dispatch("get_snapshot", {"camera_id": camera_id})
        return {
            "status": "ok",
            "snapshot_path": result.get("snapshot_path"),
            "size_bytes": result.get("size_bytes"),
        }
    except Exception as e:
        # Don't raise — that would surface as a routine failure that backs
        # off subsequent runs. Sleep monitoring is best-effort, frame-by-
        # frame; one missed capture shouldn't disable the whole loop.
        logger.warning("sleep_camera_tick: capture failed (camera %s): %s", camera_id, e)
        return {"status": "error", "error": str(e), "camera_id": camera_id}


def sleep_camera_tick_local(*, target_date=None, routine=None):
    """Capture one frame from a configured LAN RTSP camera (Tapo / Wyze /
    Reolink etc.) for sleep monitoring. Parallel to ``sleep_camera_tick``
    which uses the Ring bridge — switch to this one when a higher-res
    local camera is in place.

    The camera_alias comes from the routine spec (``spec.camera_alias``).
    The local_camera_snapshot tool handles ffmpeg + event publishing; the
    sleep_analyzer subscriber picks up the resulting frame via the new
    ``kind: "bedroom"`` filter.
    """
    spec = (routine.spec if routine and isinstance(getattr(routine, "spec", None), dict) else {}) or {}
    camera_alias = str(spec.get("camera_alias") or "").strip()
    if not camera_alias:
        logger.error("sleep_camera_tick_local: spec.camera_alias is required.")
        return {"status": "error", "error": "missing camera_alias in routine spec"}
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage
        cls = DI.tool_registry.get_tool_class("local_camera_snapshot")
        if cls is None:
            return {"status": "error", "error": "local_camera_snapshot tool not registered"}
        msg = ToolMessage(
            tool_name="local_camera_snapshot",
            tool_data={"arguments": {"camera_alias": camera_alias}},
        )
        result = cls().execute(msg)
        if getattr(result, "result_type", "") == "error":
            return {"status": "error", "error": getattr(result, "content", "tool failed")}
        data = getattr(result, "data", {}) or {}
        return {
            "status": "ok",
            "snapshot_path": data.get("snapshot_path"),
            "size_bytes": data.get("size_bytes"),
            "camera_alias": camera_alias,
        }
    except Exception as e:
        logger.warning("sleep_camera_tick_local: capture failed (alias %s): %s", camera_alias, e)
        return {"status": "error", "error": str(e), "camera_alias": camera_alias}


ROUTINE_FUNCTION_REGISTRY = {
    "dayflow_orchestrator_cadence_tick": dayflow_orchestrator_cadence_tick,
    "situation_audit": situation_audit,
    "location_refresh": location_refresh,
    "chat_memory_index": chat_memory_index,
    "proposal_promoter": proposal_promoter,
    "wiki_nightly_refresh": wiki_nightly_refresh,
    "wiki_growth": wiki_growth,
    "kg_finding_backlog_drain": kg_finding_backlog_drain,
    "kg_date_gap_drain": kg_date_gap_drain,
    "kg_state_decay": kg_state_decay,
    "kg_finding_cluster_resolve": kg_finding_cluster_resolve,
    "kg_finding_executor_drain": kg_finding_executor_drain,
    "kg_state_date_drain": kg_state_date_drain,
    "kg_importance_rater": kg_importance_rater,
    "sleep_camera_tick": sleep_camera_tick,
    "sleep_camera_tick_local": sleep_camera_tick_local,
}
