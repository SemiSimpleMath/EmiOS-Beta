"""Room ROOM.md body parsing — unrecognized H1 sections must NOT be silently dropped.

Regression for a real bug: Slack rooms authored personality + an Engagement Policy
under descriptive `# H1` headers that weren't in the 6-name recognized set, so the
loader silently dropped them — the chat_gate prompt lost the assistant's personality and the
"default action is no-op / only speak when addressed" guidance. The fix folds an
unknown section into the most-recent recognized key instead of dropping it.
"""
from __future__ import annotations

from app.assistant.rooms.room_resource_loader import _split_body_into_blackboard


def test_unknown_section_folds_into_preceding_recognized_key():
    body = (
        "# Identity\n"
        "You are the assistant.\n\n"
        "# Your personality/backstory\n"
        "Warm and casual.\n\n"
        "# Conversation\n"
        "Speak when spoken to.\n\n"
        "# Engagement Policy (Highest Priority)\n"
        "Default action is no-op.\n\n"
        "# Safety\n"
        "Be safe.\n"
    )
    bb = _split_body_into_blackboard(body)

    # Personality folds into identity (the recognized section above it).
    assert "You are the assistant" in bb["room_identity"]
    assert "Warm and casual" in bb["room_identity"]
    assert "personality/backstory" in bb["room_identity"]  # header preserved
    # Engagement Policy folds into conversation.
    assert "Speak when spoken to" in bb["room_conversation"]
    assert "Default action is no-op" in bb["room_conversation"]
    assert "Engagement Policy" in bb["room_conversation"]
    # Recognized sections still route where they always did.
    assert "Be safe" in bb["room_safety"]
    assert "Default action is no-op" not in bb["room_safety"]


def test_orphan_section_before_any_recognized_is_dropped():
    body = (
        "# Scratch note\n"
        "internal todo\n\n"
        "# Identity\n"
        "You are the assistant.\n"
    )
    bb = _split_body_into_blackboard(body)
    assert "internal todo" not in bb.get("room_identity", "")
    assert "You are the assistant" in bb["room_identity"]


def test_canonical_only_room_is_unchanged():
    body = (
        "# Identity\nI am here.\n\n"
        "# Conversation\nBe brief.\n\n"
        "# Safety\nNo secrets.\n"
    )
    bb = _split_body_into_blackboard(body)
    assert bb["room_identity"] == "# Identity\nI am here."
    assert bb["room_conversation"] == "# Conversation\nBe brief."
    assert bb["room_safety"] == "# Safety\nNo secrets."
