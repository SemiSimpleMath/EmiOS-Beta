"""Work from the brain's list: the list-item intake node (docs/design/brain_delegates_work_2026-10-04.md, section 3).

Pins: an entry decided needed gets a work object shell built by
code (objective = the outcome and when, the brief, the entry id and its belief or concern in the constraints) and
each change is acted on once; a changed entry with active work is revised and a re-plan instruction left on it in
the same commit (any change the list records, the assistant's part included); an entry
decided not needed, or cancelled, sends an end instruction with the brain's reason; an entry still decided needed
after its work ended gets new work (2026-10-08); an entry made before its kind's work came from the list (a day's
before CALENDAR_FROM_DAY, a concern's before CONCERN_SINCE) is left as it was, the retired steward's; a calendar
concern's entry carries its concern and its belief (2026-10-09). The calendar
entries here (`cal:<day>:<belief>`) are written as the day's tasks wrote them until 2026-10-09; intake still carries
the ones made before. Scratch list and deliveries; the dayflow test store; the brief and the register are fakes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.dayflow_orchestrator import brain_list_intake as B
from app.assistant.subconscious import brain_list as L
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
DAY = "2026-03-10"


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


REGISTER = {"active": [{"concern_id": "c-ring", "title": "Doorbell battery"},
                       {"concern_id": "c-trash", "title": "The trash is at the curb before bed", "belief_refs": ["B7"],
                        "calendar": {"day": DAY, "belief_key": "B7", "time": "9:00 PM"}}],
            "addressing": [], "resolved": [], "dormant": []}


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "brief", lambda entry: f"BRIEF of {entry['id']}")
    monkeypatch.setattr(B, "CALENDAR_FROM_DAY", DAY)
    monkeypatch.setattr(B, "CONCERN_SINCE", NOW.isoformat())
    from app.assistant.subconscious import concern_store
    monkeypatch.setattr(concern_store, "load_register", lambda connect=None: REGISTER)
    return sqlite_file(tmp_path / "list.db"), sqlite_file(tmp_path / "deliveries.db")


def _cal(day, belief):
    return f"cal:{day}:{belief}"


def _decide(lst, decision="needed", when="9:00 PM", day=DAY, at=NOW, belief="B3", part="set the AC to 70"):
    """A calendar entry, as the day's tasks wrote them until 2026-10-09."""
    L.ensure_schema(lst)
    needed = decision == "needed"
    with lst(True) as c:
        return L.upsert(c, {"id": _cal(day, belief), "kind": "calendar", "day": day, "belief_key": belief,
                            "placement_time": "9:00 PM", "concern_id": None,
                            "what": part if needed else "The owner wants the AC at 70.", "decision": decision,
                            "who_acts": "", "assistant_part": part if needed else "", "when_local": when if needed else "",
                            "covers": [], "why": "A standing wish."}, cause="test", at=at.isoformat())


def _ring(lst, changes, at=NOW):
    """The list decider's changes to the doorbell concern's entries (brain_list.apply_entries)."""
    return L.apply_entries(DAY, [{**c, "concern_id": "c-ring", "relies_on": []} for c in changes], cause="decided",
                           at=at, connect=lst)


def _deliver(scratch, at=NOW):
    lst, dl = scratch
    return B.deliver(_store(), now_utc=at, connect=dl, list_connect=lst)


def test_a_needed_entry_becomes_a_shell_once(scratch):
    _decide(scratch[0])
    out = _deliver(scratch)
    [work_id] = out["created"]
    wo = _store().load(work_id)
    c = wo.constraints
    assert c["list_entry_id"] == _cal(DAY, "B3") and c["belief_refs"] == ["B3"] and c["concern_refs"] == []
    # dated (2026-10-08): the calendar day, and when the entry was decided
    assert c["objective"] == "set the AC to 70 (for Tue 03-10; when: 9:00 PM; decided Tue 03-10 08:00 AM)"
    assert c["rationale"] == f"BRIEF of {c['list_entry_id']}"
    assert wo.status == "active" and not any(wo.is_work_unit(n) for n in wo.nodes.values()), "the architect plans it"
    assert _deliver(scratch)["created"] == [], "a change is acted on once"


def test_a_changed_entry_revises_its_work_and_leaves_a_durable_replan(scratch):
    _decide(scratch[0])
    [work_id] = _deliver(scratch)["created"]
    _decide(scratch[0], when="8:30 PM", at=NOW + timedelta(minutes=5))
    out = _deliver(scratch, at=NOW + timedelta(minutes=6))
    assert out["revised"] == [work_id]
    c = _store().load(work_id).constraints
    assert c["objective"] == "set the AC to 70 (for Tue 03-10; when: 8:30 PM; decided Tue 03-10 08:05 AM)"
    [instruction] = c["architect_instructions"]
    assert instruction["kind"] == "replan" and instruction["from"] == "brain" and not instruction.get("consumed_at")


def test_a_change_of_the_assistants_part_alone_revises_the_work(scratch):
    """Until 2026-10-05 only a changed what or when counted: the objective is what + when, and the assistant's part
    of a calendar entry is its what, but a concern entry's part is not, and the change was recorded delivered."""
    _decide(scratch[0])
    [work_id] = _deliver(scratch)["created"]
    _decide(scratch[0], part="tell the owner the AC is set to 70", at=NOW + timedelta(minutes=5))
    assert _deliver(scratch, at=NOW + timedelta(minutes=6))["revised"] == [work_id]


def test_not_needed_or_cancelled_ends_the_work_with_the_brains_reason(scratch):
    lst, _ = scratch
    _decide(lst)
    [work_id] = _deliver(scratch)["created"]
    _decide(lst, decision="not_needed", at=NOW + timedelta(minutes=5))
    assert _deliver(scratch)["ended"] == [work_id]
    [instruction] = _store().load(work_id).constraints["architect_instructions"]
    assert instruction["reason"] == "The brain: A standing wish." and instruction["from"] == "brain"
    _decide(lst, belief="B4")
    [other] = _deliver(scratch)["created"]
    with lst(True) as c:
        L.cancel(c, _cal(DAY, "B4"), reason="the owner is away tonight", cause="decided", at=NOW)
    assert _deliver(scratch)["ended"] == [other], "a cancelled entry ends its work"


def test_an_entry_still_needed_after_its_work_ended_gets_new_work(scratch):
    """2026-10-08: the architect ended the evening's phone-charge reminder as a duplicate of the day before's, the
    brain re-decided the entry needed, and intake only recorded it: no work, no reminder. The brain's decision goes
    through."""
    lst, _ = scratch
    _decide(lst)
    [work_id] = _deliver(scratch)["created"]
    store = _store()
    store.apply("set_work_status", {"work_id": work_id, "status": "abandoned", "reason": "duplicate"}, actor="test")
    _decide(lst, when="8:30 PM", at=NOW + timedelta(minutes=5))
    out = _deliver(scratch)
    assert out["revised"] == [] and len(out["created"]) == 1 and out["created"][0] != work_id
    assert "8:30 PM" in store.load(out["created"][0]).title


def test_an_entry_made_before_its_kinds_start_stays_as_it_was(scratch):
    """A day before CALENDAR_FROM_DAY, and a concern's entry made before CONCERN_SINCE, were the steward's: never
    made into work by the list, so old entries cannot suddenly become work."""
    lst, _ = scratch
    _decide(lst, day="2026-03-09")
    _ring(lst, [{"entry": "new", "action": "new", "what": "The doorbell battery is recharged",
                 "assistant_part": "ask the owner to confirm", "when": "9:00 AM", "why": "Low battery."}],
          at=NOW - timedelta(minutes=1))
    assert _deliver(scratch) == {"created": [], "revised": [], "ended": [], "failed": []}
    assert _store().list_work_objects() == []


def test_a_concern_entry_carries_its_concern(scratch, monkeypatch):
    lst, _ = scratch
    from app.assistant.subconscious import concern_feedback
    monkeypatch.setattr(concern_feedback, "propagate_work_outcome", lambda store, work_id, outcome: None)
    _ring(lst, [{"entry": "new", "action": "new", "what": "The doorbell battery is recharged",
                 "assistant_part": "tell the owner and ask him to confirm", "when": "9:00 AM",
                 "why": "Ring sent a low-battery notice."}])
    [work_id] = _deliver(scratch)["created"]
    assert _store().load(work_id).constraints["concern_refs"] == ["concern:c-ring"]


def test_a_calendar_concerns_entry_carries_its_concern_and_its_belief(scratch, monkeypatch):
    """Rule 7: a placement is a concern; the work made for its entry returns its outcome to the concern and to the
    belief it came from."""
    lst, _ = scratch
    from app.assistant.subconscious import concern_feedback
    monkeypatch.setattr(concern_feedback, "propagate_work_outcome", lambda store, work_id, outcome: None)
    L.apply_entries(DAY, [{"entry": "new", "action": "new", "what": "The trash is at the curb before bed",
                           "assistant_part": "remind the owner", "when": "9:00 PM", "why": "Trash night.",
                           "concern_id": "c-trash", "relies_on": []}], cause="decided", at=NOW, connect=lst)
    [work_id] = _deliver(scratch)["created"]
    c = _store().load(work_id).constraints
    assert c["concern_refs"] == ["concern:c-trash"] and c["belief_refs"] == ["B7"]


def test_a_concern_entrys_part_changed_alone_revises_its_work(scratch, monkeypatch):
    """The review's case (2026-10-05): "ask the owner" became "wait for the confirmation without contacting the
    owner", the outcome and the time unchanged; the objective (what + when) was the same, and the change was recorded
    delivered with nothing done."""
    lst, _ = scratch
    from app.assistant.subconscious import concern_feedback
    monkeypatch.setattr(concern_feedback, "propagate_work_outcome", lambda store, work_id, outcome: None)
    entry = {"entry": "new", "action": "new", "what": "The doorbell battery is recharged",
             "assistant_part": "ask the owner to confirm", "when": "9:00 AM", "why": "Ring sent a low-battery notice."}
    _ring(lst, [entry])
    [work_id] = _deliver(scratch)["created"]
    [listed] = L.entries(concern_id="c-ring", connect=lst)
    _ring(lst, [{**entry, "entry": listed["id"], "action": "modify",
                 "assistant_part": "wait for the Ring confirmation without contacting the owner"}],
          at=NOW + timedelta(minutes=5))
    assert _deliver(scratch, at=NOW + timedelta(minutes=6))["revised"] == [work_id]


def test_a_closed_concern_cancels_its_open_entries_and_its_work_ends(scratch, monkeypatch):
    """Until 2026-10-05 a resolution touched no entry: the brain held the matter settled while dayflow kept an active
    goal for it."""
    lst, _ = scratch
    from app.assistant.subconscious import concern_feedback
    monkeypatch.setattr(concern_feedback, "propagate_work_outcome", lambda store, work_id, outcome: None)
    _ring(lst, [{"entry": "new", "action": "new", "what": "The doorbell battery is recharged",
                 "assistant_part": "ask the owner to confirm", "when": "9:00 AM", "why": "Low battery."}])
    [work_id] = _deliver(scratch)["created"]
    register = {"active": [], "addressing": [], "dormant": [],
                "resolved": [{"concern_id": "c-ring", "resolution_reason": "The owner recharged it this morning."}]}
    later = NOW + timedelta(minutes=5)
    [cancelled] = L.cancel_for_closed_concerns(register, at=later, connect=lst)
    assert L.entries(concern_id="c-ring", connect=lst) == []
    assert L.cancel_for_closed_concerns(register, at=later, connect=lst) == [], "once"
    assert _deliver(scratch, at=later)["ended"] == [work_id]
    [instruction] = _store().load(work_id).constraints["architect_instructions"]
    assert instruction["kind"] == "end" and "The owner recharged it this morning." in instruction["reason"]


def test_one_entry_that_fails_is_left_for_the_next_tick_and_the_others_go_on(scratch, monkeypatch):
    lst, _ = scratch
    _decide(lst, belief="B3")
    _decide(lst, belief="B4")
    real = B.brief

    def brief(entry):
        if entry["belief_key"] == "B3":
            raise KeyError("no day's-tasks run decided B3")
        return real(entry)
    monkeypatch.setattr(B, "brief", brief)
    out = _deliver(scratch)
    assert out["failed"] == [_cal(DAY, "B3")] and len(out["created"]) == 1
    monkeypatch.setattr(B, "brief", real)
    retry = _deliver(scratch)
    assert retry["failed"] == [] and len(retry["created"]) == 1, "the failed entry is delivered on the next tick"
