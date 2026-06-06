"""bluesky_reply hydration gate — replying is consequential.

The agent must OPEN the post it's replying to (bluesky_hydrate_post) first: that
confirms it's the right target and that the agent read the full content (the
timeline list is a truncated preview). bluesky_reply refuses a ref that hasn't
been hydrated this session; the flag lives in the ref-map entry, so a fresh
bluesky_timeline (which rebuilds the map) resets it.

Tool-level tests with mocked DI + network. Run:
  .venv\\Scripts\\python.exe -m pytest app/assistant/tests/tool_tests/test_bluesky_reply_hydration_gate.py
"""
from __future__ import annotations

from types import SimpleNamespace

import app.assistant.lib.tools.bluesky_hydrate_post.bluesky_hydrate_post as hyd_mod
import app.assistant.lib.tools.bluesky_reply.bluesky_reply as reply_mod
from app.assistant.lib.tools.bluesky_timeline.bluesky_timeline import TIMELINE_REF_KEY


class _BB:
    """Minimal stand-in for DI.global_blackboard (returns the live stored object)."""
    def __init__(self, initial):
        self._d = dict(initial)

    def get_state_value(self, key, default=None):
        return self._d.get(key, default)

    def update_state_value(self, key, value):
        self._d[key] = value


def _entry(hydrated=False):
    e = {
        "uri": "at://did:plc:x/app.bsky.feed.post/p1", "cid": "c1",
        "root_uri": "at://did:plc:x/app.bsky.feed.post/p1", "root_cid": "c1",
        "author": "a.bsky.social", "text": "a normal text post, no image",
        "has_image": False, "image_url": None, "image_alt": None,
    }
    if hydrated:
        e["hydrated"] = True
    return e


def _msg(args):
    return SimpleNamespace(tool_data={"arguments": args})


def test_reply_blocked_until_hydrated(monkeypatch):
    bb = _BB({TIMELINE_REF_KEY: {"b1": _entry(hydrated=False)}})
    monkeypatch.setattr(reply_mod, "DI", SimpleNamespace(global_blackboard=bb))

    def _no_network(*a, **k):
        raise AssertionError("create_record must NOT run before hydration")
    monkeypatch.setattr(reply_mod, "create_record", _no_network)

    res = reply_mod.BlueskyReply().execute(_msg({"post_ref": "b1", "text": "hi there"}))
    assert res.result_type == "error"
    assert res.data["error_code"] == "post_not_hydrated"
    assert "bluesky_hydrate_post" in res.content  # actionable: tells the agent what to do


def test_hydrate_then_reply_succeeds(monkeypatch):
    bb = _BB({TIMELINE_REF_KEY: {"b1": _entry(hydrated=False)}})

    # Hydrate b1 (no image -> no download); sets the flag on the shared ref-map.
    monkeypatch.setattr(hyd_mod, "DI", SimpleNamespace(global_blackboard=bb))
    hres = hyd_mod.BlueskyHydratePost().execute(_msg({"post_ref": "b1"}))
    assert hres.result_type == "bluesky_post"
    assert bb.get_state_value(TIMELINE_REF_KEY)["b1"]["hydrated"] is True

    # Now the reply is allowed and reaches create_record.
    monkeypatch.setattr(reply_mod, "DI", SimpleNamespace(global_blackboard=bb))
    monkeypatch.setattr(reply_mod, "create_record",
                        lambda *a, **k: {"uri": "at://did:plc:x/app.bsky.feed.post/reply1"})
    monkeypatch.setattr(reply_mod, "now_iso", lambda: "2026-06-06T00:00:00.000Z")
    res = reply_mod.BlueskyReply().execute(
        _msg({"post_ref": "b1", "text": "good point, i agree on this"}))
    assert res.result_type == "bluesky_reply"
    assert res.data["reply_uri"] == "at://did:plc:x/app.bsky.feed.post/reply1"


def test_fresh_timeline_resets_hydration(monkeypatch):
    # A hydrated ref can be replied to; once a NEW timeline replaces the ref-map with
    # a fresh (un-hydrated) b1, the reply must block again — the new b1 is a different
    # post, so the old hydration must not authorize it.
    bb = _BB({TIMELINE_REF_KEY: {"b1": _entry(hydrated=True)}})
    monkeypatch.setattr(reply_mod, "DI", SimpleNamespace(global_blackboard=bb))
    monkeypatch.setattr(reply_mod, "create_record", lambda *a, **k: {"uri": "at://x/reply"})
    monkeypatch.setattr(reply_mod, "now_iso", lambda: "2026-06-06T00:00:00.000Z")

    ok = reply_mod.BlueskyReply().execute(_msg({"post_ref": "b1", "text": "x post here"}))
    assert ok.result_type == "bluesky_reply"  # hydrated -> allowed

    bb.update_state_value(TIMELINE_REF_KEY, {"b1": _entry(hydrated=False)})  # fresh timeline
    blocked = reply_mod.BlueskyReply().execute(_msg({"post_ref": "b1", "text": "x post here"}))
    assert blocked.result_type == "error"
    assert blocked.data["error_code"] == "post_not_hydrated"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
