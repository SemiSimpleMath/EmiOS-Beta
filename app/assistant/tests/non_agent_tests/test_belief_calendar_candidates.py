"""Calendar candidates (owner, 2026-10-03): after the intake, every belief not yet looked at is proposed as calendar
material or not; only the owner approves. Pins: undecided beliefs are read in pages and each decided once, a
candidate carries when it would appear (the agent's validate.py refuses an answer that breaks either; corrected once by
agent_runtime/correction.py since 2026-11-15), a decided belief is not read again, and a retired one is never read.
Scratch store; fake agent; invented data only.
"""
from __future__ import annotations

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.utils.path_utils import get_app_root
from belief_engine.intake import calendar
from belief_engine.intake.store import IntakeStore, sqlite_file


def _store(tmp_path, statements):
    store = IntakeStore(sqlite_file(tmp_path / "s.db"), "belief_intake_")
    for st in statements:
        store.apply("2026-01-01", {"statement": st, "kind": "routine_pattern", "scope": "chronic", "sources": []},
                    [1.0, 0.0], {"verdict": "new"})
    return store


def _rows(store):
    with store._connect(False) as c:
        return {r["id"]: (r["calendar"], r["calendar_note"]) for r in
                c.execute("SELECT id, calendar, calendar_note FROM belief_intake_beliefs ORDER BY rowid")}


def _agent(candidates):
    calls = []

    def call(payload):
        calls.append(payload)
        return {"decisions": [{"belief_key": b["id"], "candidate": b["id"] in candidates,
                               "when": candidates.get(b["id"], ""), "reason": "because"} for b in payload["beliefs"]]}
    call.calls = calls
    return call


def test_each_undecided_belief_is_proposed_or_not_once(tmp_path, monkeypatch):
    store = _store(tmp_path, ["The owner's household trash day is Wednesday.", "The owner likes potato balls.",
                              "The owner and Sam celebrate their dating anniversary on April 4."])
    store.retire("B2", "2026-01-02", "gone")
    monkeypatch.setattr(calendar, "PAGE_SIZE", 1)
    agent = _agent({"B1": "every Wednesday night", "B3": "April 4 each year"})
    assert calendar.tag_calendar_candidates(store, call=agent) == {"proposed": 2, "not_proposed": 0}
    assert [[b["id"] for b in p["beliefs"]] for p in agent.calls] == [["B1"], ["B3"]], "pages; a retired belief is not read"
    assert _rows(store)["B1"] == ("proposed", "every Wednesday night: because")
    assert _rows(store)["B2"] == (None, None)
    store.apply("2026-01-03", {"statement": "The owner enjoys R.E.M.", "kind": "stable_preference", "scope": "chronic",
                               "sources": []}, [0.0, 1.0], {"verdict": "new"})
    again = _agent({})
    assert calendar.tag_calendar_candidates(store, call=again) == {"proposed": 0, "not_proposed": 1}
    assert [b["id"] for b in again.calls[0]["beliefs"]] == ["B4"], "a decided belief is not read again"
    assert _rows(store)["B4"] == ("not_proposed", "because")


RULES = AgentRegistry()._load_validator(
    get_app_root() / "assistant" / "agents" / "belief_engine" / "calendar_candidate" / "validate.py")


def test_the_rules_refuse_an_unknown_or_repeated_key_a_belief_not_decided_and_a_candidate_without_when():
    given = {"beliefs": [{"id": "B1", "statement": "s"}, {"id": "B2", "statement": "s"}, {"id": "B3", "statement": "s"}]}
    ok = {"decisions": [{"belief_key": "B1", "candidate": True, "when": "Wednesday night", "reason": "r"},
                        {"belief_key": "B2", "candidate": False, "when": "", "reason": "r"},
                        {"belief_key": "B3", "candidate": False, "when": "", "reason": "r"}]}
    assert RULES.validate(ok, given) == []
    bad = RULES.validate({"decisions": [{"belief_key": "B1", "candidate": True, "when": " ", "reason": "r"},
                                        {"belief_key": "B9", "candidate": False, "reason": "r"},
                                        {"belief_key": "B2", "candidate": False, "reason": "r"},
                                        {"belief_key": "B2", "candidate": False, "reason": "r"}]}, given)
    assert [(p.item, p.at, p.value, p.structural) for p in bad] == [
        ("B1", "when", " ", False), ("", "", "B9", True), ("B2", "", None, True), ("B3", "", None, True)]
    assert "more than once" in bad[2].why and "not decided" in bad[3].why
    assert RULES.ITEM_KEYS == {"decisions": "belief_key"}


def test_an_unknown_calendar_status_is_refused(tmp_path):
    store = _store(tmp_path, ["The owner likes potato balls."])
    with pytest.raises(ValueError, match="unknown calendar status"):
        store.set_calendar("B1", "maybe", None)
