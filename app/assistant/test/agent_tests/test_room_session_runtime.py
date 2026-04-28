from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.room_session_manager.contracts import InboundEnvelope
from app.assistant.room_session_manager.room_session_manager import RoomSessionManager
from app.assistant.room_session_manager.services.room_ingress_service import RoomIngressService
from app.assistant.room_session_manager.services.post_room_service import PostRoomService
from app.assistant.room_session_manager.services.room_history_builder import RoomHistoryBuilder
from app.assistant.utils.pydantic_classes import Message


def test_room_history_builder_applies_summary_boundary(monkeypatch):
    base = datetime(2026, 2, 17, 10, 0, tzinfo=timezone.utc)
    old_msg = Message(
        data_type="user_msg",
        sender="Justin",
        role="user",
        content="older raw that should be hidden",
        is_chat=True,
        timestamp=base,
        room_id="justin",
        room_surface="slack",
        room_context_id="main",
    )
    summary = Message(
        data_type="agent_msg",
        sender="room_summary",
        content="summary content",
        is_chat=True,
        timestamp=base + timedelta(minutes=1),
        room_id="justin",
        room_surface="slack",
        room_context_id="main",
        sub_data_type=["history_summary"],
        metadata={"summarized_through_utc": (base + timedelta(minutes=2)).isoformat()},
    )
    new_msg = Message(
        data_type="user_msg",
        sender="Justin",
        role="user",
        content="new raw should appear",
        is_chat=True,
        timestamp=base + timedelta(minutes=3),
        room_id="justin",
        room_surface="slack",
        room_context_id="main",
    )

    class _Global:
        @staticmethod
        def get_recent_chat_since_utc(*args, **kwargs):
            return [old_msg, summary, new_msg]

    monkeypatch.setattr("app.assistant.room_session_manager.services.room_history_builder.DI.global_blackboard", _Global())
    out = RoomHistoryBuilder().build_context(room_id="justin", room_surface="slack", room_context_id="main", limit=40)
    assert "[Chat Summary]: summary content" in out
    assert "new raw should appear" in out
    assert "older raw that should be hidden" not in out


def test_post_room_service_builds_outbound_intent_send_flag():
    class _ManagerResult:
        data = {"final_answer_answer": "Hello from room"}
        content = None

    svc = PostRoomService()
    intent = svc.build_outbound_intent(
        request_id="rid1",
        room_id="justin",
        room_surface="slack",
        room_context_id="main",
        reply_to={"type": "slack", "channel_id": "C123"},
        manager_result=_ManagerResult(),
        send_reply=True,
    )
    assert intent.reply_text == "Hello from room"
    assert intent.send is True
    assert intent.delivery_mode == "auto_send"


def test_room_session_manager_message_persistence_mode_validation():
    mgr = RoomSessionManager()
    assert mgr._normalize_message_persistence_mode("global_blackboard_only") == "global_blackboard_only"
    assert (
        mgr._normalize_message_persistence_mode("global_blackboard_and_unified_log")
        == "global_blackboard_and_unified_log"
    )
    with pytest.raises(ValueError):
        mgr._normalize_message_persistence_mode("bad_mode")


def test_room_session_manager_room_policy_can_block_unified_log():
    mgr = RoomSessionManager()
    room_ctx = {
        "room_policy": {
            "retention": {
                "write_unified_log": False,
            }
        }
    }
    enabled, reason = mgr._resolve_room_unified_log_persistence(
        message_persistence_mode="global_blackboard_and_unified_log",
        room_ctx=room_ctx,
        room_id="random_contact",
        room_surface="sms",
    )
    assert enabled is False
    assert reason == "room_policy_blocked"


def test_room_session_manager_room_policy_allows_unified_log():
    mgr = RoomSessionManager()
    room_ctx = {
        "room_policy": {
            "retention": {
                "write_unified_log": True,
            }
        }
    }
    enabled, reason = mgr._resolve_room_unified_log_persistence(
        message_persistence_mode="global_blackboard_and_unified_log",
        room_ctx=room_ctx,
        room_id="phil",
        room_surface="sms",
    )
    assert enabled is True
    assert reason == "allowed"


def test_room_ingress_service_builds_request_data_from_envelope():
    svc = RoomIngressService()
    env = InboundEnvelope(
        surface="slack",
        room_id="justin",
        context_id="main",
        request_id="rid",
        speaker_name="Justin",
        speaker_id="slack:U1",
        speaker_external_id="U1",
        content="hi",
        timestamp_local="2026-02-17 11:00:00",
        inbound_line="[11:00] Justin: hi",
        transport_message_id="171234.0001",
        transport_from="U1",
        transport_to="C1",
        extras={"channel_id": "C1", "message_ts": "171234.0001"},
    )
    data = svc.build_request_data(
        room_ctx={"room_permissions": {"allowed_tools": ["find_tool"]}},
        envelope=env,
        room_contact_name="Justin",
        allowed_resource_context="resources",
    )
    assert data.get("room_id") == "justin"
    assert data.get("room_surface") == "slack"
    assert data.get("task_allowed_tools") == ["find_tool"]


def test_room_ingress_service_mode_routing_normal_and_planning():
    svc = RoomIngressService()
    env = InboundEnvelope(
        surface="ui",
        room_id="master_room",
        context_id="main",
        request_id="rid",
        speaker_name="User",
        speaker_id="socket:1",
        speaker_external_id="socket:1",
        content="hello",
        timestamp_local="2026-02-17 11:00:00",
        inbound_line="[11:00] User: hello",
    )
    room_ctx = {
        "flow_config": {
            "flow": {
                "normal": {"source_agent": "master_room::chat_gate"},
                "planning_mode": {"source_agent": "master_room::plan_mode"},
            }
        }
    }
    normal_data = svc.build_request_data(
        room_ctx=room_ctx,
        envelope=env,
        room_contact_name="User",
        allowed_resource_context="resources",
    )
    assert normal_data.get("room_mode") == "normal"
    assert normal_data.get("next_agent") == "master_room::chat_gate"

    env_planning = InboundEnvelope(
        surface="ui",
        room_id="master_room",
        context_id="main",
        request_id="rid2",
        speaker_name="User",
        speaker_id="socket:1",
        speaker_external_id="socket:1",
        content="continue planning",
        timestamp_local="2026-02-17 11:01:00",
        inbound_line="[11:01] User: continue planning",
        metadata={"room_mode": "planning_mode"},
    )
    planning_data = svc.build_request_data(
        room_ctx=room_ctx,
        envelope=env_planning,
        room_contact_name="User",
        allowed_resource_context="resources",
    )
    assert planning_data.get("room_mode") == "planning_mode"
    assert planning_data.get("next_agent") == "master_room::plan_mode"


def test_room_ingress_service_apply_room_mode_context_commands():
    class _StubPlanSessions:
        def activate_room_binding(self, **kwargs):
            return {"plan_session_id": "plan_test_1"}

        def deactivate_room_binding(self, **kwargs):
            return True

        def get_active_room_binding(self, **kwargs):
            return None

    svc = RoomIngressService(plan_session_service=_StubPlanSessions())

    body, meta, early = svc.apply_room_mode_context(
        room_id="master_room",
        surface="ui",
        context_id="main",
        body="/plan research eye rest techniques",
        metadata={},
        sender_identity="socket:1",
    )
    assert early is None
    assert body == "research eye rest techniques"
    assert meta.get("room_mode") == "planning_mode"
    assert meta.get("plan_session_id") == "plan_test_1"

    body_done, meta_done, early_done = svc.apply_room_mode_context(
        room_id="master_room",
        surface="ui",
        context_id="main",
        body="/done",
        metadata={},
        sender_identity="socket:1",
    )
    assert body_done == ""
    assert isinstance(meta_done, dict)
    assert isinstance(early_done, dict)
    assert "Planning mode closed." in str(early_done.get("reply_text") or "")


def test_master_room_chat_gate_seeded_history_is_bounded_to_24h(monkeypatch):
    now_utc = datetime.now(timezone.utc)
    recent = Message(
        data_type="user_msg",
        sender="User",
        role="user",
        content="recent message",
        is_chat=True,
        timestamp=now_utc - timedelta(hours=2),
        room_id="master_room",
        room_surface="ui",
        room_context_id="main",
    )
    old = Message(
        data_type="user_msg",
        sender="User",
        role="user",
        content="old message",
        is_chat=True,
        timestamp=now_utc - timedelta(hours=26),
        room_id="master_room",
        room_surface="ui",
        room_context_id="main",
    )

    mgr = RoomSessionManager()
    monkeypatch.setattr(mgr.history_builder, "build_messages", lambda **_kwargs: [old, recent])

    seeded = mgr._build_room_session_seeded_messages(
        room_id="master_room",
        limit=None,
        room_surface="ui",
        room_context_id="main",
        shared_chat_room_ids=[],
        current_room_mode="normal",
    )

    contents = [str(item.get("content") or "") for item in seeded if isinstance(item, dict)]
    assert "recent message" in contents
    assert "old message" not in contents
