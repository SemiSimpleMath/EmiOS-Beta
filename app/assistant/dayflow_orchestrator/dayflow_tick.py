"""
Dayflow orchestrator cadence tick — entry point.

The heavy lifting is in sibling modules:
- orchestrator_status.py  — status resource CRUD, master-room blocking
- blackboard_builder.py   — emits day_of_week (per-agent prep nodes own the rest)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.dayflow_orchestrator.blackboard_builder import (
    build_dayflow_blackboard_extras,
    enrich_items_with_local_times,
)
from app.assistant.dayflow_orchestrator.orchestrator_status import (
    DAYFLOW_ORCHESTRATOR_ROOM_ID,
    DAYFLOW_ORCHESTRATOR_STATUS_RESOURCE_ID,
    MASTER_ROOM_BLOCK_SECONDS,
    block_dayflow_orchestrator_for_master_chat,
    load_orchestrator_status,
    persist_orchestrator_status,
)
from app.assistant.room_session_manager.services.system_scope_builder import (
    build_system_scope_for_room,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeResourcePolicy
from app.assistant.utils.time_utils import parse_iso_utc_strict

logger = get_logger(__name__)

# Re-export for backward compatibility — these are imported by other modules
# from app.assistant.dayflow_orchestrator.dayflow_tick
__all__ = [
    "DAYFLOW_ORCHESTRATOR_ROOM_ID",
    "MASTER_ROOM_BLOCK_SECONDS",
    "block_dayflow_orchestrator_for_master_chat",
    "dayflow_orchestrator_cadence_tick",
    "enrich_items_with_local_times",
    "_enrich_items_with_local_times",
]

# Keep the underscore alias so existing `from dayflow_tick import _enrich_items_with_local_times`
# in control nodes continues to work.
_enrich_items_with_local_times = enrich_items_with_local_times


def dayflow_orchestrator_cadence_tick(*, target_date: str | None = None, routine=None) -> None:
    if target_date is not None:
        logger.warning("dayflow_orchestrator_cadence_tick: target_date='%s' is not implemented and will be ignored.", target_date)
    now_utc = datetime.now(timezone.utc)
    routine_id = str(getattr(routine, "routine_id", "") or "").strip()
    status = load_orchestrator_status()
    raw_blocked = status.get("blocked_until_utc")
    blocked_until_utc = parse_iso_utc_strict(raw_blocked, label="blocked_until_utc") if raw_blocked else None
    if blocked_until_utc is not None and now_utc < blocked_until_utc:
        status["last_skip_reason"] = "blocked_by_master_room_timer"
        status["last_skip_at_utc"] = now_utc.isoformat()
        status["last_routine_id"] = routine_id
        persist_orchestrator_status(status)
        logger.info(
            "Skipped dayflow orchestrator cadence tick: blocked until %s (routine_id=%s)",
            blocked_until_utc.isoformat(),
            routine_id,
        )
        return

    # Ingest new data from all sources (email, chat, tickets, delegation
    # requests) into the dayflow_items DB before building context.
    from app.assistant.dayflow_orchestrator.ingestion import run_dayflow_ingestion
    ingestion_summary = run_dayflow_ingestion(now_utc=now_utc)
    logger.info(
        "dayflow_orchestrator_cadence_tick: ingestion complete — %s",
        ingestion_summary,
    )

    # Close dispatches whose manager never reported back, so their source
    # items can be re-promoted to actionable on this tick. The orphan
    # sweep is the backstop that catches tasks stuck in 'dispatched'
    # without a live dispatch row pointing at them — closes the gap
    # where the dispatch row closed cleanly but the source-task revive
    # step crashed.
    from app.assistant.dayflow_orchestrator.dispatch_sweeper import (
        sweep_stale_dispatches,
        sweep_orphaned_dispatched_tasks,
    )
    sweep_stale_dispatches(now_utc=now_utc)
    sweep_orphaned_dispatched_tasks(now_utc=now_utc)

    # Build minimal extras (day_of_week). Per-agent prep nodes own the rest.
    blackboard_extras = build_dayflow_blackboard_extras()

    request_id = str(uuid.uuid4())
    scope = build_system_scope_for_room(
        room_id=DAYFLOW_ORCHESTRATOR_ROOM_ID,
        scope_id=f"dayflow_cadence::{request_id}",
        actor_id="dayflow_cadence_tick",
        surface="internal",
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )
    msg_data: Dict[str, Any] = {
        "trigger": "routine_cadence",
        "routine_id": routine_id,
    }
    if isinstance(blackboard_extras, dict):
        msg_data.update(blackboard_extras)

    msg = Message(
        event_topic="dayflow_tick",
        sender="system",
        receiver=None,
        task="Dayflow cadence tick",
        information="",
        content="",
        data=msg_data,
        request_id=request_id,
        scope_context=scope,
    )

    try:
        manager = DI.multi_agent_manager_factory.create_manager("dayflow_orchestrator_manager")
        result = DI.manager_invoker.invoke(manager, msg)
        status["last_room_request_id"] = request_id
    except Exception as e:
        logger.error("dayflow_orchestrator_cadence_tick: manager invocation failed: %s", e)
        logger.debug("cadence tick exception details", exc_info=True)
        raise

    status["last_skip_reason"] = ""
    status["last_run_at_utc"] = now_utc.isoformat()
    status["last_routine_id"] = routine_id
    persist_orchestrator_status(status)
