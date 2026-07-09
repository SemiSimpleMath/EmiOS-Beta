"""Delivery-status stamping + failed-send owner notice (delivery audit D2).

A send that exhausts the transport's retries used to persist
indistinguishably from a sent reply, and nobody was told. Now
_deliver_outbound returns (result, status), persistence stamps
delivery_status on the row, and a failure raises an owner notice ticket.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.assistant.room_session_manager.contracts import InboundEnvelope
from app.assistant.room_session_manager.room_session_manager import (
    InboundSurfaceAdapter,
    RoomSessionManager,
)
from app.assistant.utils.pydantic_classes import Message


def _envelope() -> InboundEnvelope:
    return InboundEnvelope(
        surface="slack",
        room_id="justin",
        context_id="main",
        request_id="rid-1",
        speaker_name="Justin",
        speaker_id="slack:U1",
        speaker_external_id="U1",
        content="hi",
        timestamp_local="2026-07-08 21:00:00",
        inbound_line="[21:00] Justin: hi",
        transport_message_id="1.0",
        transport_from="U1",
        transport_to="C1",
    )


def _adapter(send_fn, captured: dict) -> InboundSurfaceAdapter:
    def _append_outbound(reply_text, outbound_result):
        msg = Message(data_type="user_msg", content=reply_text, role="assistant")
        captured["outbound_msg"] = msg
        return msg

    return InboundSurfaceAdapter(
        inbound_source="room_slack",
        outbound_source="room_slack",
        append_inbound=lambda: None,
        append_outbound=_append_outbound,
        send_outbound=send_fn,
    )


def _rsm(captured: dict) -> RoomSessionManager:
    rsm = RoomSessionManager.__new__(RoomSessionManager)
    rsm._persist_message_to_unified_log = (
        lambda *, message, source: captured.setdefault("persisted", []).append((message, source))
    )
    return rsm


def test_failed_send_stamps_status_and_raises_owner_notice(monkeypatch):
    tickets = []
    import app.assistant.ticket_manager.ticket_service as ts
    monkeypatch.setattr(ts, "propose_notice_ticket", lambda **kw: tickets.append(kw) or "t1")

    captured: dict = {}
    rsm = _rsm(captured)

    def _boom(_text):
        raise RuntimeError("transport exhausted retries")

    result, status = rsm._deliver_outbound(
        envelope=_envelope(), adapter=_adapter(_boom, captured),
        reply_text="hello there", should_send=True,
    )
    assert result is None and status == "failed"
    assert len(tickets) == 1
    assert tickets[0]["suggestion_type"] == "delivery_failure"
    assert "justin" in tickets[0]["title"]

    rsm._persist_outbound_turn(
        adapter=_adapter(_boom, captured), persist_unified_log=True,
        reply_text="hello there", outbound_result=result, delivery_status=status,
    )
    assert captured["outbound_msg"].metadata["delivery_status"] == "failed"
    assert captured["persisted"], "row still persists (the record survives) with the failed stamp"


def test_sent_and_skipped_statuses(monkeypatch):
    import app.assistant.ticket_manager.ticket_service as ts
    monkeypatch.setattr(ts, "propose_notice_ticket", lambda **kw: "t1")

    captured: dict = {}
    rsm = _rsm(captured)
    ok_adapter = _adapter(lambda _text: "sid-123", captured)

    result, status = rsm._deliver_outbound(
        envelope=_envelope(), adapter=ok_adapter, reply_text="hi", should_send=True,
    )
    assert (result, status) == ("sid-123", "sent")

    result, status = rsm._deliver_outbound(
        envelope=_envelope(), adapter=ok_adapter, reply_text="hi", should_send=False,
    )
    assert (result, status) == (None, "skipped")

    rsm._persist_outbound_turn(
        adapter=ok_adapter, persist_unified_log=False,
        reply_text="hi", outbound_result="sid-123", delivery_status="sent",
    )
    assert captured["outbound_msg"].metadata["delivery_status"] == "sent"
