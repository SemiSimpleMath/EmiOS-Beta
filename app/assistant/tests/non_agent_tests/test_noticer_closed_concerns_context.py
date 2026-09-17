"""The noticer must see the concerns it already closed (2026-09-14).

A concern was resolved on one tick and re-minted under a fresh UUID on the
next, because the context rendered `active` + `addressing` only. The evidence
that produced the concern is still in context for days after the decision --
the friction aggregate spans 14 days -- so the noticer re-derived it, and
persist's concern_id dedup never fires against a brand-new UUID.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.subconscious.context_builder import (
    _CLOSED_CONCERN_WINDOW_DAYS,
    _build_concerns_recently_closed,
    build_noticer_context,
)


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _concern(cid: str, title: str, **over):
    base = {
        "concern_id": cid,
        "title": title,
        "subject": "the user",
        "kind": "pattern_drift",
        "horizon": "this_week",
        "severity": "high",
    }
    base.update(over)
    return base


def test_recent_resolution_is_rendered_with_its_reason():
    register = {
        "active": [],
        "addressing": [],
        "resolved": [
            _concern("c-sleep", "Fatigue and poor sleep have recurred this week",
                     resolved_at_utc=_iso(1),
                     resolution_reason="User said the 4 AM waking sorted itself out."),
        ],
        "dormant": [],
    }
    out = _build_concerns_recently_closed(register)
    assert "c-sleep" in out
    assert "Fatigue and poor sleep have recurred this week" in out
    # The reason is the whole point: it is what tells the noticer whether new
    # evidence actually contradicts the decision.
    assert "4 AM waking sorted itself out" in out


def test_closure_older_than_the_window_is_dropped():
    register = {
        "active": [], "addressing": [], "dormant": [],
        "resolved": [
            _concern("c-old", "Long settled", resolved_at_utc=_iso(_CLOSED_CONCERN_WINDOW_DAYS + 1),
                     resolution_reason="stopped"),
            _concern("c-fresh", "Just settled", resolved_at_utc=_iso(_CLOSED_CONCERN_WINDOW_DAYS - 1),
                     resolution_reason="stopped"),
        ],
    }
    out = _build_concerns_recently_closed(register)
    assert "c-fresh" in out
    assert "c-old" not in out


def test_dormant_never_ages_out():
    """accept_chronic is a standing decision -- a sleep concern archived months
    ago is still the reason not to re-mint it today."""
    register = {
        "active": [], "addressing": [], "resolved": [],
        "dormant": [
            _concern("c-chronic", "Sleep comments are a recurring pattern",
                     chronic=True, dormant_at_utc=_iso(120),
                     dormant_reason="Long-term pattern, user aware."),
        ],
    }
    out = _build_concerns_recently_closed(register)
    assert "c-chronic" in out
    assert "chronic" in out.lower()
    assert "Long-term pattern, user aware." in out


def test_undated_closure_is_listed_not_dropped():
    """Showing one stale closure costs a line of prompt; hiding a fresh one
    costs the duplicate this section exists to prevent."""
    register = {
        "active": [], "addressing": [], "dormant": [],
        "resolved": [
            _concern("c-nodate", "No timestamp recorded", resolution_reason="settled"),
            _concern("c-bad", "Unparseable timestamp", resolved_at_utc="not-a-date",
                     resolution_reason="settled"),
        ],
    }
    out = _build_concerns_recently_closed(register)
    assert "c-nodate" in out
    assert "c-bad" in out


def test_empty_register_says_so_without_raising():
    out = _build_concerns_recently_closed({"active": [], "addressing": [],
                                           "resolved": [], "dormant": []})
    assert out == "(nothing closed recently)"


def test_noticer_context_exposes_the_key_the_config_declares():
    """The prompt interpolates {{ concerns_recently_closed }}; the context dict
    must carry that exact key or the section renders empty and the loop returns."""
    import yaml
    from app.assistant.utils.path_utils import get_repo_root

    config = yaml.safe_load(
        (get_repo_root() / "app/assistant/agents/subconscious/noticer/config.yaml")
        .read_text(encoding="utf-8")
    )
    assert "concerns_recently_closed" in config["user_context_items"]

    context = build_noticer_context()
    missing = [k for k in config["user_context_items"] if k not in context]
    assert missing == []
