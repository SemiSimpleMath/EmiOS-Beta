"""
Tests for ``find_calendar_event_by_name`` — the semantic-similarity variant
of calendar event search.

NOTE on status: this tool is shipped DISABLED in the alpha package. A
``.disabled`` sentinel file in the tool's directory prevents the registry
from loading it, because it imports sentence-transformers (HuggingFace)
which is excluded from ``requirements.txt``. These tests auto-skip
when the ``.disabled`` file is present so they don't break the fast test
run; when someone flips to the full requirements and removes the sentinel,
they'll run and validate correctness.

The lightweight alternative — substring search via
``get_calendar_events(event_name_contains=...)`` — is covered in
``test_get_calendar_events_tool.py``. That path does NOT require
sentence-transformers and is what the planner should prefer for the
common "find the X event" query pattern.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401


_TOOL_DIR = REPO_ROOT / "app" / "assistant" / "lib" / "tools" / "find_calendar_event_by_name"
_DISABLED_SENTINEL = _TOOL_DIR / ".disabled"

pytestmark = pytest.mark.skipif(
    _DISABLED_SENTINEL.exists(),
    reason=(
        "find_calendar_event_by_name is disabled in this package "
        "(.disabled sentinel present — requires sentence-transformers). "
        "Install full requirements and remove the sentinel to enable."
    ),
)


# --------------------------------------------------------------------
# Helpers — these only get imported/executed when the tool is enabled.
# --------------------------------------------------------------------

def _build_tool(monkeypatch):
    """Build a FindCalendarEventByName instance with its network path stubbed.

    The tool inherits from CalendarTool so we bypass __init__ (no real
    auth / DB / event hub) and install the same no-op doubles that the
    other calendar tool tests use.
    """
    from app.assistant.tests.tool_tests._helpers import install_calendar_tool_test_doubles
    from app.assistant.lib.tools.find_calendar_event_by_name.find_calendar_event_by_name import (
        FindCalendarEventByName,
    )
    tool = FindCalendarEventByName.__new__(FindCalendarEventByName)
    tool.name = "find_calendar_event_by_name"
    tool.tool_name = "find_calendar_event_by_name"
    install_calendar_tool_test_doubles(tool, monkeypatch)
    # init_credentials is a no-op for tests since service is already stubbed
    monkeypatch.setattr(tool, "init_credentials", lambda *a, **k: None, raising=False)
    return tool


def _install_stub_get_events(monkeypatch, events):
    from app.assistant.lib.core_tools.calendar_tool import calendar_tool as ct
    monkeypatch.setattr(ct, "get_events", lambda **_: events, raising=True)


def _base_event(*, event_id, summary, description=""):
    return {
        "id": event_id,
        "summary": summary,
        "description": description,
        "location": "",
        "start": {"dateTime": "2026-04-24T20:00:00Z"},
        "end": {"dateTime": "2026-04-24T22:00:00Z"},
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
    }


def _install_stub_embedder(monkeypatch):
    """Replace the embedder with a deterministic bag-of-words vectorizer so
    tests don't need a real embedding model.

    Cosine similarity between two texts becomes a simple shared-token count
    after normalization — 'friday night meats' vs 'Friday Night Meats' ≈ 1.0,
    'friday night meats' vs 'coffee with martin' ≈ 0.0. Good enough to
    validate the ranking and top-k selection logic without HuggingFace.
    """
    import numpy as np
    from app.assistant.lib.tools.find_calendar_event_by_name import (
        find_calendar_event_by_name as fcebn,
    )

    # Fixed vocab size for deterministic vectors — doesn't matter what, as
    # long as the same token always maps to the same index.
    def _bag_of_words(text: str):
        tokens = [t.strip().lower() for t in str(text or "").split() if t.strip()]
        counts: dict[str, int] = {}
        for t in tokens:
            counts[t] = counts.get(t, 0) + 1
        # Hash tokens into a 1024-dim vector.
        vec = np.zeros(1024, dtype=float)
        for t, c in counts.items():
            vec[hash(t) % 1024] += c
        return vec.tolist()

    monkeypatch.setattr(fcebn, "embed_text", _bag_of_words, raising=True)


# --------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------

def test_returns_top_match_by_title(monkeypatch):
    """The closest-matching title should be ranked first."""
    _install_stub_embedder(monkeypatch)
    tool = _build_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(event_id="a", summary="Coffee with Martin"),
        _base_event(event_id="b", summary="Friday Night Meats"),
        _base_event(event_id="c", summary="Analytics team standup"),
    ])

    from app.assistant.utils.pydantic_classes import ToolMessage
    msg = ToolMessage(
        tool_name="find_calendar_event_by_name",
        tool_data={"arguments": {"event_name": "friday night meats"}},
    )
    result = tool.execute(msg)
    assert result.result_type == "success"
    assert len(result.data_list) >= 1
    assert result.data_list[0]["summary"] == "Friday Night Meats"


def test_returns_top_5_at_most(monkeypatch):
    """When more than 5 unique titles exist, only top 5 are returned."""
    _install_stub_embedder(monkeypatch)
    tool = _build_tool(monkeypatch)
    events = [
        _base_event(event_id=f"e{i}", summary=f"Event Title {i}")
        for i in range(12)
    ]
    _install_stub_get_events(monkeypatch, events)

    from app.assistant.utils.pydantic_classes import ToolMessage
    msg = ToolMessage(
        tool_name="find_calendar_event_by_name",
        tool_data={"arguments": {"event_name": "Event Title 3"}},
    )
    result = tool.execute(msg)
    assert len(result.data_list) <= 5


def test_missing_event_name_returns_error(monkeypatch):
    tool = _build_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [])

    from app.assistant.utils.pydantic_classes import ToolMessage
    msg = ToolMessage(
        tool_name="find_calendar_event_by_name",
        tool_data={"arguments": {}},  # no event_name
    )
    result = tool.execute(msg)
    assert result.result_type == "error"
    assert "event_name" in (result.content or "")


def test_zero_events_returns_empty_success(monkeypatch):
    _install_stub_embedder(monkeypatch)
    tool = _build_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [])

    from app.assistant.utils.pydantic_classes import ToolMessage
    msg = ToolMessage(
        tool_name="find_calendar_event_by_name",
        tool_data={"arguments": {"event_name": "anything"}},
    )
    result = tool.execute(msg)
    assert result.result_type == "success"
    assert result.data_list == []


def test_dedupes_duplicate_titles_before_scoring(monkeypatch):
    """Multiple instances of the same recurring title should not inflate
    the candidate set — one representative per unique title.
    """
    _install_stub_embedder(monkeypatch)
    tool = _build_tool(monkeypatch)
    _install_stub_get_events(monkeypatch, [
        _base_event(event_id="a1", summary="Friday Night Meats"),
        _base_event(event_id="a2", summary="Friday Night Meats"),
        _base_event(event_id="a3", summary="Friday Night Meats"),
        _base_event(event_id="b", summary="Coffee with Martin"),
    ])

    from app.assistant.utils.pydantic_classes import ToolMessage
    msg = ToolMessage(
        tool_name="find_calendar_event_by_name",
        tool_data={"arguments": {"event_name": "friday"}},
    )
    result = tool.execute(msg)
    titles = [e["summary"] for e in result.data_list]
    assert titles.count("Friday Night Meats") == 1


def test_content_includes_event_id_and_meeting_link(monkeypatch):
    """content (agent-visible) must carry the event id + meeting link so the
    agent can act on the match without re-scanning the calendar."""
    _install_stub_embedder(monkeypatch)
    tool = _build_tool(monkeypatch)
    ev = _base_event(event_id="fnm-id-1", summary="Friday Night Meats")
    ev["conferenceData"] = {"entryPoints": [
        {"entryPointType": "video", "uri": "https://zoom.us/j/99999999999"},
    ]}
    _install_stub_get_events(monkeypatch, [ev])

    from app.assistant.utils.pydantic_classes import ToolMessage
    msg = ToolMessage(
        tool_name="find_calendar_event_by_name",
        tool_data={"arguments": {"event_name": "friday night meats"}},
    )
    result = tool.execute(msg)
    assert "id=fnm-id-1" in result.content
    assert "meeting_link=https://zoom.us/j/99999999999" in result.content
