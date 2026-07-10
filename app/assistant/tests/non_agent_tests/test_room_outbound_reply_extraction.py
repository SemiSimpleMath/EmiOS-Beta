"""Outbound reply text comes only from the contract (room-session-manager RSM2, 2026-07-09).

extract_reply_text used to join ALL manager `data` values into the reply when the
final-answer schema was absent — leaking internal manager fields into chat/slack.
Now text reaches a user only via final_answer_answer or the manager's own
`content`; a bare data dict yields no reply.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.assistant.room_session_manager.services.post_room_service import PostRoomService


def _result(data=None, content=None):
    return SimpleNamespace(data=data, content=content)


class TestExtractReplyText:

    def test_final_answer_is_returned(self):
        text, payload = PostRoomService.extract_reply_text(
            _result(data={"final_answer_answer": "hello there"})
        )
        assert text == "hello there"
        assert payload == {"final_answer_answer": "hello there"}

    def test_internal_data_is_not_serialized_into_reply(self):
        # No final_answer, no content — the internal keys must NOT become the reply.
        data = {"internal_state": "secret", "debug_dump": {"k": "v"}, "step": 3}
        text, payload = PostRoomService.extract_reply_text(_result(data=data))
        assert text == ""
        assert payload == data  # payload still carries the data; only the TEXT is suppressed

    def test_empty_final_answer_yields_no_reply_and_no_leak(self):
        # Schema present but empty, alongside internal keys → no reply, no leak.
        data = {"final_answer_answer": "", "internal_state": "secret"}
        text, _ = PostRoomService.extract_reply_text(_result(data=data))
        assert text == ""

    def test_no_op_contract_yields_no_reply(self):
        data = {"final_answer_no_op": True, "note": "internal"}
        text, _ = PostRoomService.extract_reply_text(_result(data=data))
        assert text == ""

    def test_content_is_used_when_no_final_answer(self):
        text, _ = PostRoomService.extract_reply_text(
            _result(data={"misc": "x"}, content="from content channel")
        )
        assert text == "from content channel"

    def test_content_only_result(self):
        text, payload = PostRoomService.extract_reply_text(_result(content="just content"))
        assert text == "just content"
        assert payload == {}

    def test_empty_everything_yields_no_reply(self):
        text, payload = PostRoomService.extract_reply_text(_result())
        assert text == ""
        assert payload == {}
