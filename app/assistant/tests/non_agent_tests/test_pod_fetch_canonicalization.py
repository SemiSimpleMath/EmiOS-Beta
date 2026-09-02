"""pod_fetch id canonicalization: a transcribed pod id must not read as "missing".

Agents copy pod ids between prompts and tool calls, and the observed slip is a
dropped ``datapod:<kind>:`` prefix — the 2026-09-01 SLMS syllabus episode: the
worker fetched ``6f7f901e...`` bare, the exact-match store said "missing" for a
pod it held, and the deliverable shipped without the one document that mattered.

Rule pinned here: exact hit untouched; a miss with EXACTLY one id-join candidate
(final segment equality or prefix either direction) is repaired and reported; a
miss with several candidates is never guessed — it stays missing and comes back
as a "did you mean" suggestion list; a miss with none stays a plain miss. A
repaired id passes through the same scope wall as an exact one.

Hermetic by construction: a stub store, no database, no DI state. A test in this
repo must NEVER write fixture rows through the app session (the seed-row lesson).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.assistant.lib.core_tools.pod_store.pod_store_tool import PodStoreTool
from app.assistant.utils.pydantic_classes import ToolMessage


def _pod(pod_id: str, scope_id: str = "google_user_primary"):
    return SimpleNamespace(
        pod_id=pod_id, kind="email", tags=[], one_liner="stub",
        scope_id=scope_id, created_by="test", created_at=datetime(2026, 9, 1),
        body="the syllabus body", source_refs=[], for_agents=[], metadata={},
        min_authority=0, importance=None,
    )


class _StubStore:
    def __init__(self, *pod_ids: str):
        self._pods = {p: _pod(p) for p in pod_ids}

    def get(self, pod_id: str):
        return self._pods.get(pod_id)

    def all_pod_ids(self):
        return list(self._pods)


def _tool(store) -> PodStoreTool:
    tool = PodStoreTool.__new__(PodStoreTool)   # skip BaseTool wiring — unit scope
    tool._store = store
    return tool


def _fetch(tool, ids, scope_context=None):
    tm = ToolMessage(tool_name="pod_fetch", tool_data={"pod_ids": ids},
                     scope_context=scope_context)
    res = tool.handle_pod_fetch({"pod_ids": ids}, tm)
    return res.data, str(getattr(res, "content", "") or "")


CANON = "datapod:email:6f7f901ec26cb4adf53413c8"


def test_exact_id_is_untouched():
    tool = _tool(_StubStore(CANON))
    data, _ = _fetch(tool, [CANON])
    assert len(data["pods"]) == 1 and not data["missing"]
    assert "repaired_ids" not in data


def test_bare_suffix_is_repaired_and_fetches():
    """The SLMS regression: the bare hash must hydrate, loudly."""
    tool = _tool(_StubStore(CANON))
    data, content = _fetch(tool, ["6f7f901ec26cb4adf53413c8"])
    assert len(data["pods"]) == 1, "bare suffix must repair to the canonical id"
    assert data["missing"] == []
    assert data["repaired_ids"] == {"6f7f901ec26cb4adf53413c8": CANON}
    assert "repaired" in content, "the agent must be TOLD the id was repaired"


def test_wrong_kind_prefix_is_repaired():
    """Same hash filed under the wrong kind still joins on the final segment."""
    tool = _tool(_StubStore(CANON))
    data, _ = _fetch(tool, ["datapod:research_finding:6f7f901ec26cb4adf53413c8"])
    assert len(data["pods"]) == 1 and data["missing"] == []


def test_ambiguous_match_is_never_guessed():
    a = "datapod:email:aaaa11112222"
    b = "datapod:chat_cluster:aaaa11112222"
    tool = _tool(_StubStore(a, b))
    data, content = _fetch(tool, ["aaaa11112222"])
    assert data["pods"] == [], "two candidates is not a slip — never pick one"
    assert data["missing"] == ["aaaa11112222"]
    assert sorted(data["did_you_mean"]["aaaa11112222"]) == sorted([a, b])
    assert "Did you mean" in content


def test_unknown_id_stays_a_plain_miss():
    tool = _tool(_StubStore(CANON))
    data, content = _fetch(tool, ["deadbeef0000"])
    assert data["pods"] == [] and data["missing"] == ["deadbeef0000"]
    assert "did_you_mean" not in data and "repaired_ids" not in data


def test_repaired_id_still_hits_the_scope_wall():
    """Canonicalization restores identity, not access: a repaired id from a scope
    the caller cannot read is masked exactly like an exact-id cross-scope fetch."""
    tool = _tool(_StubStore(CANON))
    narrow_scope = {"owner_id": "test", "actor_id": "test", "surface": "test",
                    "scope_id": "scope::test", "room_id": "some_room",
                    "pods": {"allowed_scopes": ["self"]}}
    data, _ = _fetch(tool, ["6f7f901ec26cb4adf53413c8"], scope_context=narrow_scope)
    assert data["pods"] == []
    assert data["missing"] == [CANON], "repair happens, the scope wall still holds"
