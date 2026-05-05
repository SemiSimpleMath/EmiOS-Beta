"""Tests for RoomMentionRouter: parse, resolve, post-to-mailbox, early reply."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.assistant.room_session_manager.services.room_mention_router import (
    RoomMentionRouter,
    MentionResult,
)


@pytest.fixture(autouse=True)
def _seed_display_names():
    """Populate the display-name registry for every test in this module."""
    from app.assistant.chat_narrator.display_names import (
        initialize_display_name_registry,
    )
    initialize_display_name_registry({
        "web_manager": "Quimby",
        "emi_team_manager": "Em",
        "personal_admin_manager": "Phyllis",
    })
    yield
    initialize_display_name_registry({})


def _make_router(*, active_workers=None):
    """Build a router with mocked DI dependencies."""
    workers = active_workers or []
    by_display = {w["display_name"].lower(): w for w in workers}

    def fake_resolve(name):
        return by_display.get((name or "").lower())

    def fake_list_active():
        return list(workers)

    def fake_list_display_names():
        return [w["display_name"] for w in workers]

    mock_mailbox = MagicMock()
    mock_mailbox.post.return_value = True

    router = RoomMentionRouter(
        active_workers_provider=fake_list_active,
        worker_resolver=fake_resolve,
        display_names_lister=fake_list_display_names,
        mailbox=mock_mailbox,
    )
    return router, mock_mailbox


# ── extract_mention parsing ────────────────────────────────────────


class TestExtractMention:

    def test_at_with_body(self):
        assert RoomMentionRouter.extract_mention("@em abort") == ("em", "abort")

    def test_at_with_leading_whitespace(self):
        assert RoomMentionRouter.extract_mention("   @quimby   focus on yamaha") == (
            "quimby", "focus on yamaha",
        )

    def test_at_only_no_body(self):
        assert RoomMentionRouter.extract_mention("@em") == ("em", "")

    def test_at_uppercase_preserved(self):
        # Case preservation is expected; resolver does case-insensitive matching.
        assert RoomMentionRouter.extract_mention("@EM stop") == ("EM", "stop")

    def test_no_at_returns_none(self):
        assert RoomMentionRouter.extract_mention("hello em") is None

    def test_at_in_middle_returns_none(self):
        assert RoomMentionRouter.extract_mention("hi @em how are you") is None

    def test_empty_string(self):
        assert RoomMentionRouter.extract_mention("") is None
        assert RoomMentionRouter.extract_mention("   ") is None


# ── route: no mention → None (caller continues normal pipeline) ───


class TestRouteNoMention:

    def test_plain_text_returns_none(self):
        router, mb = _make_router()
        result = router.route(body="just chatting", room_id="master_room", meta={})
        assert result is None
        mb.post.assert_not_called()


# ── route: unknown name → None (caller treats as plain chat) ──────


class TestRouteUnknownName:

    def test_unknown_at_name_returns_none(self):
        router, mb = _make_router()
        # @steve isn't in the seeded display registry.
        result = router.route(body="@steve hey", room_id="master_room", meta={})
        assert result is None
        mb.post.assert_not_called()


# ── route: known name but no active worker → friendly early reply ─


class TestRouteKnownNameButInactive:

    def test_returns_short_circuit_result(self):
        router, mb = _make_router(active_workers=[])
        result = router.route(body="@quimby check savory", room_id="master_room", meta={})
        assert isinstance(result, MentionResult)
        assert result.continue_pipeline is False
        text = result.early_result["reply_text"]
        assert "No active Quimby" in text
        mb.post.assert_not_called()

    def test_includes_other_active_workers_hint(self):
        active = [{"display_name": "Phyllis", "invocation_id": "inv-phy",
                   "manager_name": "personal_admin_manager"}]
        router, mb = _make_router(active_workers=active)
        result = router.route(body="@quimby check", room_id="master_room", meta={})
        assert "active right now: Phyllis" in result.early_result["reply_text"]


# ── route: active worker + body → posts to mailbox + acks ─────────


class TestRouteActiveWorker:

    def test_posts_to_mailbox_with_planner_role(self):
        active = [{"display_name": "Quimby", "invocation_id": "inv-Q-1",
                   "manager_name": "web_manager"}]
        router, mb = _make_router(active_workers=active)
        result = router.route(body="@quimby focus on yamaha", room_id="master_room", meta={})

        # One mailbox post, addressed to the planner role.
        mb.post.assert_called_once()
        call = mb.post.call_args
        assert call.kwargs["invocation_id"] == "inv-Q-1"
        assert call.kwargs["message_type"] == "agent_inject"
        assert "planner" in call.kwargs["payload"]
        # Body wrapped with sender header.
        wrapped = call.kwargs["payload"]["planner"]
        assert "focus on yamaha" in wrapped
        assert "User:" in wrapped
        assert "+++++" in wrapped

        # User-facing ack.
        assert result.continue_pipeline is False
        assert "Quimby noted" in result.early_result["reply_text"]
        assert result.early_result["sender_display_name"] == "Quimby"

    def test_case_insensitive_at_name(self):
        active = [{"display_name": "Em", "invocation_id": "inv-E",
                   "manager_name": "emi_team_manager"}]
        router, mb = _make_router(active_workers=active)
        router.route(body="@EM abort", room_id="master_room", meta={})
        mb.post.assert_called_once()

    def test_empty_body_does_not_post(self):
        active = [{"display_name": "Quimby", "invocation_id": "inv-Q",
                   "manager_name": "web_manager"}]
        router, mb = _make_router(active_workers=active)
        result = router.route(body="@quimby", room_id="master_room", meta={})
        mb.post.assert_not_called()
        assert "didn't say anything" in result.early_result["reply_text"]
        assert result.continue_pipeline is False
