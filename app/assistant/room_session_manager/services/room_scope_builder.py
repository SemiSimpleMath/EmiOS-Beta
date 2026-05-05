"""
Room scope contract builder.

Builds the canonical ScopeContext dict for a room request from
room context, envelope, and request data.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict

from app.assistant.room_session_manager.contracts import InboundEnvelope
from app.assistant.room_session_manager.services.room_policy_service import resolve_room_authority_level
from app.assistant.manager_runtime.services.scope_adapter import (
    _SCOPE_HISTORY_LOOKBACK_HOURS,
)
from app.assistant.utils.pydantic_classes import ScopeContext


def build_scope_contract_for_room_request(
        *,
        room_ctx: Dict[str, Any],
        envelope: InboundEnvelope,
        request_data: Dict[str, Any],
) -> Dict[str, Any]:
    room_policy = room_ctx.get("room_policy") if isinstance(room_ctx.get("room_policy"), dict) else {}
    room_permissions = room_ctx.get("room_permissions") if isinstance(room_ctx.get("room_permissions"), dict) else {}
    room_access = room_ctx.get("room_access") if isinstance(room_ctx.get("room_access"), dict) else {}
    retention = room_policy.get("retention") if isinstance(room_policy.get("retention"), dict) else {}
    delivery = room_policy.get("delivery") if isinstance(room_policy.get("delivery"), dict) else {}

    tool_classes = room_permissions.get("tool_classes") if isinstance(room_permissions.get("tool_classes"), dict) else {}
    task_allowed_tools = request_data.get("task_allowed_tools", None)
    if task_allowed_tools is None:
        resolved_allowed_tools = ["all"]
    elif isinstance(task_allowed_tools, list):
        resolved_allowed_tools = list(task_allowed_tools)
    else:
        raise ValueError("task_allowed_tools must be a list when provided.")

    task_except_tools = request_data.get("task_except_tools", None)
    if task_except_tools is None:
        resolved_blocked_tools = []
    elif isinstance(task_except_tools, list):
        resolved_blocked_tools = list(task_except_tools)
    else:
        raise ValueError("task_except_tools must be a list when provided.")

    # ``reply_to`` is the per-request transport-reply dict each surface
    # builds (UI: {type=socketio, room_id}; slack: {type=slack, channel_id,
    # thread_ts, ...}; etc.). Carried through scope so chained
    # sub-managers and outbound publishers can route back without consulting
    # any ingress-specific state.
    raw_reply_to = request_data.get("reply_to")
    reply_to = dict(raw_reply_to) if isinstance(raw_reply_to, dict) else None

    scope_dict: Dict[str, Any] = {
        "schema_version": "scope_context_v1",
        "scope_id": f"scope::{envelope.surface}::{envelope.room_id}::{envelope.context_id or 'main'}::{uuid.uuid4().hex[:8]}",
        "owner_id": str(envelope.room_id or "").strip() or "unknown_owner",
        "actor_id": str(envelope.speaker_external_id or envelope.speaker_name or "unknown_actor").strip(),
        "surface": str(envelope.surface or "").strip() or "unknown",
        "room_id": str(envelope.room_id or "").strip() or None,
        "room_context_id": str(envelope.context_id or "main").strip(),
        "visibility": str((room_ctx.get("room_visibility") or "room_shared")).strip(),
        "policy_id": str((room_ctx.get("room_policy_id") or f"room_policy::{envelope.room_id}")).strip(),
        "reply_to": reply_to,
        "history": {
            "mode": "summary_plus_recent",
            "source": "unified_log",
            "include_room_scoped": True,
            "lookback_hours": _SCOPE_HISTORY_LOOKBACK_HOURS,
            "max_messages": None,
            "max_chars_per_message": None,
        },
        "resources": {
            "allowed_global_resources": list(room_ctx.get("room_allowed_global_resources") or []) or ["all"],
            "allowed_room_resources": [],
            "denied_resources": [],
            "resource_groups": list(room_ctx.get("room_rag_scopes") or []),
        },
        "tools": {
            "allowed_tools": resolved_allowed_tools,
            "blocked_tools": resolved_blocked_tools,
            "requires_approval_tools": [],
            "allow_external_side_effects": bool(tool_classes.get("external_action", False)),
        },
        "entities": {
            "enabled": True,
            "allowed_entity_cards": list(room_access.get("allowed_entity_cards") or []),
            "pinned_entities": list(room_access.get("pinned_entities") or []),
            "entity_lookback_messages": None,
            "entity_lookback_seconds": None,
        },
        "cards": {"enabled": True, "allowed_cards": [], "max_cards_per_turn": None, "max_total_chars": None},
        "writes": {
            "write_unified_log": bool(retention.get("write_unified_log", True)),
            "write_kg": bool(retention.get("write_kg", False)),
            "allow_fact_extraction": bool(retention.get("allow_fact_extraction", False)),
            "writable_state_keys": [],
        },
        "delivery": {
            "auto_send": bool(delivery.get("auto_send", True)),
            "allow_initiation": bool(delivery.get("allow_initiation", False)),
            "allowed_reply_types": [str(envelope.surface or "").strip()] if str(envelope.surface or "").strip() else [],
        },
        "approval": {
            "authority_level": resolve_room_authority_level(
                room_policy=room_policy,
                surface=str(envelope.surface or "").strip(),
            ),
        },
        "retention": {
            "persist_chat": bool(retention.get("persist_chat", True)),
            "persist_tool_results": True,
            "allow_context_summarization": True,
            "redact_before_persist": False,
        },
        "execution": {"max_turns": None, "max_tool_calls": None, "timeout_seconds": None, "allowed_models": []},
        "delegation": {},
    }
    parsed = ScopeContext.model_validate(scope_dict)
    return parsed.model_dump()
