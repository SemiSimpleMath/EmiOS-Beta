"""R1 reliability spine: a crash in the turn body must fail LOUD to the user, never go silent.

Before R1, an exception anywhere in _handle_inbound_generic's turn body propagated to the
fire-and-forget daemon worker (process_request._run_room_inbound), whose only handler logs and
dies -> the user watched the assistant go silent forever. These tests pin the new behavior: the wrapper
catches the crash, delivers a short error reply on the SAME transport exactly once, and returns a
coherent (non-raising) turn response. They isolate the wrap by stubbing the pre-turn setup calls.
"""
from __future__ import annotations

from app.assistant.room_session_manager.room_session_manager import RoomSessionManager
from app.assistant.room_session_manager.contracts import InboundEnvelope


class _FakeAdapter:
    def __init__(self):
        self.sent = []
        self.persist_to_history = False          # so _persist_outbound_turn is a no-op (no DB)
        self.outbound_result_key = "outbound_status"
        self.include_dry_run = False
        self.outbound_source = "test_outbound"

    def send_outbound(self, reply_text):
        self.sent.append(reply_text)
        return {"ok": True}

    def append_outbound(self, reply_text, outbound_result):  # unreached (persist_to_history False)
        return None


def _raise(exc):
    def _f(**kwargs):
        raise exc
    return _f


def _envelope(request_id="abc12345xyz"):
    return InboundEnvelope(
        surface="ui",
        room_id="test_room",   # non-master -> skips the master_room ingress pre-block
        context_id="main",
        request_id=request_id,
        speaker_name="tester",
        speaker_id="tester",
        speaker_external_id=None,
        content="hello",
        timestamp_local="2026-06-09T00:00:00",
        inbound_line="hello",
        transport_message_id="m1",
        transport_from="tester",
        transport_to="assistant",
    )


def _manager_with_crashing_body(exc):
    rsm = RoomSessionManager.__new__(RoomSessionManager)   # bypass __init__/DI
    # Pre-turn setup (outside the wrap) -> no-ops so the test isolates the turn body.
    rsm._register_reply_route = lambda **k: None
    rsm._persist_inbound_turn = lambda **k: None
    rsm._get_room_handler = lambda room_ctx: None
    # The turn body raises here:
    rsm._prepare_turn_context = _raise(exc)
    return rsm


def test_turn_crash_surfaces_error_reply_not_silence():
    rsm = _manager_with_crashing_body(RuntimeError("manager exploded"))
    adapter = _FakeAdapter()

    resp = rsm._handle_inbound_generic(
        envelope=_envelope(),
        room_ctx={},
        room_contact_name="tester",
        send_reply=True,
        message_persistence_mode="none",
        persist_unified_log=False,
        persist_reason="test",
        adapter=adapter,
    )

    # Did NOT raise; produced a coherent response carrying an informative error reply.
    assert isinstance(resp, dict)
    assert "RuntimeError" in resp["reply_text"]
    # Delivered exactly once, on the same transport.
    assert adapter.sent == [resp["reply_text"]]
    assert resp["reply_payload"].get("error") is True
    assert resp["reply_payload"].get("error_type") == "RuntimeError"


def test_turn_crash_respects_send_reply_false():
    rsm = _manager_with_crashing_body(ValueError("boom"))
    adapter = _FakeAdapter()

    resp = rsm._handle_inbound_generic(
        envelope=_envelope(),
        room_ctx={},
        room_contact_name="tester",
        send_reply=False,
        message_persistence_mode="none",
        persist_unified_log=False,
        persist_reason="test",
        adapter=adapter,
    )

    # send_reply False -> nothing pushed to the transport, but still a coherent (non-raising) response.
    assert adapter.sent == []
    assert "ValueError" in resp["reply_text"]
