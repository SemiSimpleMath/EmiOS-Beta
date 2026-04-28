"""
Dayflow orchestrator status resource CRUD.

Manages the persistent status resource that tracks block timers,
skip reasons, run timestamps, and chat ingestion watermarks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

DAYFLOW_ORCHESTRATOR_ROOM_ID = "dayflow_orchestrator"
DAYFLOW_ORCHESTRATOR_STATUS_RESOURCE_ID = "resource_dayflow_orchestrator_status"
DAYFLOW_ORCHESTRATOR_INPUT_RESOURCE_ID = "resource_dayflow_orchestrator_input_messages"
HEALTH_INFERENCE_OUTPUT_RESOURCE_ID = "resource_health_inference_output"
MASTER_ROOM_BLOCK_SECONDS = 180
CHAT_WATERMARK_KEY = "chat_ingested_up_to_utc"


def _status_scope_context() -> Dict[str, Any]:
    return {
        "resources": {
            "allowed_global_resources": [DAYFLOW_ORCHESTRATOR_STATUS_RESOURCE_ID],
            "allowed_room_resources": [],
            "denied_resources": [],
            "resource_groups": [],
        }
    }


def _health_scope_context() -> Dict[str, Any]:
    return {
        "resources": {
            "allowed_global_resources": [HEALTH_INFERENCE_OUTPUT_RESOURCE_ID],
            "allowed_room_resources": [],
            "denied_resources": [],
            "resource_groups": [],
        }
    }


def load_orchestrator_status() -> Dict[str, Any]:
    resource_manager = getattr(DI, "resource_manager", None)
    if resource_manager is None:
        raise RuntimeError("resource_manager service is not registered.")
    payload = resource_manager.get_resource(
        scope_context=_status_scope_context(),
        resource_id=DAYFLOW_ORCHESTRATOR_STATUS_RESOURCE_ID,
        required=False,
    )
    if payload is None:
        return {"schema_version": 1}
    if not isinstance(payload, dict):
        raise ValueError(
            f"{DAYFLOW_ORCHESTRATOR_STATUS_RESOURCE_ID} must be a JSON object, got {type(payload).__name__}"
        )
    return dict(payload)


def persist_orchestrator_status(status: Dict[str, Any]) -> None:
    if not isinstance(status, dict):
        raise ValueError("orchestrator status payload must be an object.")
    resource_manager = getattr(DI, "resource_manager", None)
    if resource_manager is None:
        raise RuntimeError("resource_manager service is not registered.")
    payload = dict(status)
    payload["schema_version"] = 1
    payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    resource_manager.update_resource(
        DAYFLOW_ORCHESTRATOR_STATUS_RESOURCE_ID,
        payload,
        persist=True,
    )


def load_health_status_summary() -> str:
    """Return the single health summary line for dayflow agents."""
    resource_manager = getattr(DI, "resource_manager", None)
    if resource_manager is None:
        raise RuntimeError("resource_manager service is not registered.")

    payload = resource_manager.get_resource(
        scope_context=_health_scope_context(),
        resource_id=HEALTH_INFERENCE_OUTPUT_RESOURCE_ID,
        required=False,
    )
    if payload is None:
        return ""
    if not isinstance(payload, dict):
        raise ValueError(
            f"{HEALTH_INFERENCE_OUTPUT_RESOURCE_ID} must be a JSON object, got {type(payload).__name__}"
        )
    return str(payload.get("general_health_assessment") or "").strip()


def block_dayflow_orchestrator_for_master_chat(*, request_id: str) -> Dict[str, Any]:
    now_utc = datetime.now(timezone.utc)
    blocked_until_utc = now_utc + timedelta(seconds=MASTER_ROOM_BLOCK_SECONDS)
    status = load_orchestrator_status()
    status["blocked_until_utc"] = blocked_until_utc.isoformat()
    status["block_source_room_id"] = "master_room"
    status["block_source_request_id"] = str(request_id or "").strip()
    status["last_master_room_user_chat_utc"] = now_utc.isoformat()
    status["last_skip_reason"] = "master_room_recent_activity"
    persist_orchestrator_status(status)
    return status
