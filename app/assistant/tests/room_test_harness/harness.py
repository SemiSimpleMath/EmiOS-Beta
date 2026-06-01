"""Room test harness — run a real inbound through the full room pipeline.

The entry point ``simulate_room_inbound`` builds a synthetic InboundEnvelope,
loads the target room's ROOM.md, builds the real scope contract (so
per_manager rules, blocked_tools, authority_level all apply as in prod),
creates the room's manager, and invokes it via ManagerInvoker.

Side-effect isolation:
- DB writes redirect to a tempfile sqlite via ``sandboxed_di`` — real emi.db
  is never touched.
- LLM calls are REAL (the point is testing real planner decisions). Cost
  observed post-mortem from the sandbox llm_call_log table.
- External tools (send_email, http_request, etc.) currently run as-is. Tests
  that need to trigger side-effect tools should either avoid them via scope
  (per_manager rules) or wait for the stubs library (deferred).

Usage::

    from app.assistant.tests.room_test_harness.harness import simulate_room_inbound

    result = simulate_room_inbound(
        surface="slack",
        room_id="slack/__test__",
        content="What's the weather in Helsinki?",
        speaker_name="TestFriend",
        speaker_external_id="U_test",
    )
    print(result.final_response)
    print(f"LLM cost: ${result.total_cost_usd():.4f}")
    for call in result.llm_calls:
        print(call)
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.assistant.tests.room_test_harness.recorders import (
    HarnessResult,
    LLMCallRecord,
)
from app.assistant.tests.room_test_harness.sandbox_setup import sandboxed_di
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _build_envelope(
    *,
    surface: str,
    room_id: str,
    content: str,
    speaker_name: str,
    speaker_external_id: str,
    context_id: str = "main",
):
    """Construct a minimal InboundEnvelope for the harness."""
    from app.assistant.room_session_manager.contracts import InboundEnvelope
    now = datetime.now(timezone.utc)
    return InboundEnvelope(
        surface=surface,
        room_id=room_id,
        context_id=context_id,
        speaker_id=speaker_external_id,
        speaker_name=speaker_name,
        speaker_external_id=speaker_external_id,
        content=content,
        request_id=f"req_e2e_{uuid.uuid4().hex[:10]}",
        timestamp_local=now.isoformat(),
        inbound_line=f"{speaker_name}: {content}",
        transport_message_id=None,
        transport_from=None,
        transport_to=None,
    )


def _resolve_manager_name(room_ctx: dict) -> str:
    """Pick the room's manager name. Mirrors ``resolve_room_manager_name``."""
    from app.assistant.room_session_manager.services.room_policy_service import (
        resolve_room_manager_name,
    )
    return resolve_room_manager_name(room_ctx)


def _collect_llm_calls_from_sandbox(since_utc: datetime) -> list[LLMCallRecord]:
    """Query the sandbox llm_call_log for calls made during this turn."""
    from app.models.base import get_session
    from app.models.llm_call_log import LLMCallLog
    s = get_session()
    try:
        rows = (
            s.query(LLMCallLog)
            .filter(LLMCallLog.ts_utc >= since_utc)
            .order_by(LLMCallLog.ts_utc)
            .all()
        )
        return [
            LLMCallRecord(
                agent_name=str(r.agent_name or "?"),
                engine=str(r.engine or ""),
                input_tokens=int(r.input_tokens or 0),
                output_tokens=int(r.output_tokens or 0),
                cached_tokens=int(r.cached_tokens or 0),
                cost_usd=float(r.total_cost_usd or 0.0),
                duration_ms=int(r.duration_ms or 0),
            )
            for r in rows
        ]
    finally:
        s.close()


def simulate_room_inbound(
    *,
    surface: str,
    room_id: str,
    content: str,
    speaker_name: str = "TestSpeaker",
    speaker_external_id: str = "U_test",
    context_id: str = "main",
    setup_fn: Optional[callable] = None,
) -> HarnessResult:
    """Run one inbound message through the full room → manager pipeline.

    Wraps the run in a sandbox (tempfile DB) so no real DB is touched.

    Args:
        surface: ``"slack"`` / ``"ui"`` / etc. Matches an InboundEnvelope.surface.
        room_id: the room whose ROOM.md is loaded (e.g. ``"slack/__test__"``).
        content: the user's message text.
        speaker_name: display name of the speaker (default ``"TestSpeaker"``).
        speaker_external_id: surface-native speaker id (default ``"U_test"``).
        context_id: sub-room context, usually ``"main"``.

    Returns:
        HarnessResult with final_response, manager_result, scope_dict,
        llm_calls (collected post-mortem from sandbox DB), and elapsed_ms.

    Raises if room_id has no ROOM.md, the manager can't be created, or the
    invocation hits an unhandled exception. The sandbox is torn down either
    way.
    """
    from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
    from app.assistant.room_session_manager.services.room_scope_builder import (
        build_scope_contract_for_room_request,
    )
    from app.assistant.utils.pydantic_classes import Message, ToolResult
    from app.assistant.ServiceLocator.service_locator import DI

    with sandboxed_di():
        if setup_fn is not None:
            # Hook for pre-seeding sandbox state (pods, ResourceManager values,
            # KG nodes, etc.) before the manager runs. Runs INSIDE the sandbox
            # so writes land in the tempfile DB.
            setup_fn()

        room_ctx = load_room_context_for_manager(room_id)
        if not room_ctx:
            raise ValueError(f"Room {room_id!r} has no ROOM.md or it's empty.")

        envelope = _build_envelope(
            surface=surface, room_id=room_id, content=content,
            speaker_name=speaker_name, speaker_external_id=speaker_external_id,
            context_id=context_id,
        )

        # Drive the REAL ingress path (RoomIngressService) rather than a
        # hand-built Message. This is what resolves the room's flow mode and
        # seeds request_data["next_agent"] = flow_config.flow[mode].source_agent
        # — without it, managers with a mode-based flow (e.g. master_room_manager,
        # which starts at master_room::chat_gate) abort at cycle 0 in the
        # delegator. Mirrors room_session_manager.py's build_request_data ->
        # set scope_contract -> build_manager_request -> invoke_room_manager.
        from app.assistant.room_session_manager.services.room_ingress_service import (
            RoomIngressService,
        )
        ingress = RoomIngressService(
            multi_agent_manager_factory=DI.multi_agent_manager_factory,
            manager_invoker=DI.manager_invoker,
            manager_registry=getattr(DI, "manager_registry", None),
        )

        request_data = ingress.build_request_data(
            room_ctx=room_ctx,
            envelope=envelope,
            room_contact_name=speaker_name,
            allowed_resource_context="",
        )

        # Build the real scope contract for this room — per_manager rules,
        # blocked_tools, authority_level, etc. all apply exactly as in prod.
        # Set it on request_data the same way production does (after
        # build_request_data, before build_manager_request).
        scope_dict = build_scope_contract_for_room_request(
            room_ctx=room_ctx,
            envelope=envelope,
            request_data=request_data,
        )
        request_data["scope_contract"] = scope_dict

        manager_name = _resolve_manager_name(room_ctx)
        logger.info("[harness] room=%s manager=%s content=%r", room_id, manager_name, content)

        msg = ingress.build_manager_request(envelope=envelope, request_data=request_data)

        t0 = time.monotonic()
        turn_start = datetime.now(timezone.utc)
        manager_result = ingress.invoke_room_manager(manager_name=manager_name, manager_request=msg)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if isinstance(manager_result, ToolResult):
            final_response: Optional[str] = manager_result.content
        elif manager_result is None:
            final_response = None
        else:
            final_response = str(manager_result)

        llm_calls = _collect_llm_calls_from_sandbox(since_utc=turn_start)

        return HarnessResult(
            final_response=final_response,
            manager_result=manager_result,
            scope_dict=scope_dict,
            llm_calls=llm_calls,
            elapsed_ms=elapsed_ms,
        )
