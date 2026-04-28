"""System-internal ScopeContext builder.

System callers — cadence ticks, orchestrator health probes, schedulers — drive
a room without going through ``room_session_manager``. They still need their
``ScopeContext`` to honor that room's ``policy.json`` (authority_level,
retention, delivery). Historically they constructed ``ScopeContext`` directly
and forgot to set ``approval.authority_level``, which defaulted to 0 and
caused unwanted approval prompts for automated room tools.

Use ``build_system_scope_for_room`` from any system-internal caller that needs
a ScopeContext for a specific room. It loads the room's ``policy.json`` and
applies the room's authority level. Caller supplies scope_id, actor_id, and
any overrides (resources, tools, etc.).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.rooms.room_resource_loader import load_room_policy
from app.assistant.room_session_manager.services.room_policy_service import (
    resolve_room_authority_level,
)
from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
    ScopeRetentionPolicy,
    ScopeDeliveryPolicy,
)


def build_system_scope_for_room(
    *,
    room_id: str,
    scope_id: str,
    actor_id: str,
    surface: str = "internal",
    owner_id: str = "system",
    resources: Optional[ScopeResourcePolicy] = None,
) -> ScopeContext:
    """Return a ScopeContext whose approval / retention / delivery reflect the
    room's ``policy.json``.

    Parameters
    ----------
    room_id
        Room identifier. Must correspond to ``app/assistant/rooms/<room_id>/policy.json``.
    scope_id
        Caller-chosen unique scope id for this request.
    actor_id
        Identity of the system actor driving the scope (e.g. ``"dayflow_cadence_tick"``).
    surface
        Transport surface. Defaults to ``"internal"`` for system-driven work.
    owner_id
        Owning tenant. Defaults to ``"system"`` for orchestrators.
    resources
        Optional override for the scope's resource policy. When omitted, no
        resources are explicitly whitelisted (subject to default policy).
    """
    policy: Dict[str, Any] = load_room_policy(room_id)

    authority_level = resolve_room_authority_level(
        room_policy=policy,
        surface=str(surface or "").strip(),
    )

    retention = policy.get("retention") if isinstance(policy.get("retention"), dict) else {}
    delivery = policy.get("delivery") if isinstance(policy.get("delivery"), dict) else {}

    return ScopeContext(
        scope_id=scope_id,
        owner_id=owner_id,
        actor_id=actor_id,
        surface=surface,
        room_id=room_id,
        approval=ScopeApprovalPolicy(authority_level=authority_level),
        retention=ScopeRetentionPolicy(
            persist_chat=bool(retention.get("persist_chat", True)),
            persist_tool_results=True,
            allow_context_summarization=True,
            redact_before_persist=False,
        ),
        delivery=ScopeDeliveryPolicy(
            auto_send=bool(delivery.get("auto_send", True)),
            allow_initiation=bool(delivery.get("allow_initiation", False)),
            allowed_reply_types=[],
        ),
        resources=resources if resources is not None else ScopeResourcePolicy(),
    )
