"""Every item of the day has an owner (docs/design/day_items_have_owners_2026-10-02.md).

Pins: the day pass's placed beliefs nothing owns go through the concern door together as standing
candidates; where they land keeps their belief keys, so the next pass finds the owner by code with no
door call; each plan line names its owner; today's times are recorded on the owner. A standing concern
is never resolved (brain step, noticer); its brief is not rewritten for daily churn, only when an
occurrence goes wrong; its occurrences are handed to dayflow one by one, once each, while its brief says
act_now, and not when an hour past. Scratch register and plan store; the door judge is a fake. Invented
data only.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.assistant.tests.test_setup  # noqa: F401
import app.assistant.subconscious.day_pass as drs
from app.assistant.subconscious import concern_brief, concern_door, concern_handoff, day_plan, persist
from app.assistant.tests.concern_store_helpers import ScratchRegister
from app.assistant.utils.time_utils import get_local_timezone
from belief_engine.intake.store import sqlite_file

TZ = get_local_timezone()
EMPTY = {"active": [], "addressing": [], "resolved": [], "dormant": []}
BELIEFS = [{'belief_key': 'B1', 'statement': 'Set the AC to 70F every night at 9 PM.', 'status': 'active'},
           {'belief_key': 'B2', 'statement': 'Wants the AC at 70F by 9 PM.', 'status': 'active'},
           {'belief_key': 'B3', 'statement': 'Friday Night Meats is not about food.', 'status': 'active'}]


def _judge(*answers):
    calls = []

    def call(payload):
        calls.append(payload)
        return {"decisions": [{"candidate": c, "decision": d, "same_as": s, "reason": "because"}
                              for c, d, s in answers]}
    call.calls = calls
    return call


@pytest.fixture
def day(monkeypatch, tmp_path):
    import app.assistant.scope.loader as loader
    monkeypatch.setattr(drs, '_read_daily_context', lambda: {'expected_schedule': [], 'milestones': []})
    monkeypatch.setattr(drs, '_read_belief_entries', lambda: [dict(b) for b in BELIEFS])
    monkeypatch.setattr(drs, '_format_weekly_insights', lambda: '')
    monkeypatch.setattr(loader, 'load_scope_for_source', lambda **k: None)
    monkeypatch.setattr(drs, '_select_beliefs', lambda *a, **k: ([dict(b) for b in BELIEFS], 'relevant', []))
    monkeypatch.setattr(drs, '_write', lambda agent_input: {'placements': [
        {'belief_key': 'B1', 'time': '9:00 PM'}, {'belief_key': 'B2', 'time': '9:00 PM'},
        {'belief_key': 'B3', 'time': 'all day'}]})
    monkeypatch.setattr(drs, '_last_attempt_utc', None)
    return SimpleNamespace(reg=ScratchRegister(tmp_path).write(EMPTY), plans=sqlite_file(tmp_path / 'plan.db'),
                           now=datetime(2026, 1, 1, 13, 0, tzinfo=TZ).astimezone(timezone.utc))


def _lines(plans):
    plan = day_plan.load_plan('2026-01-01', connect=plans)
    return [line for s in plan['slots'] for line in s['lines']] + plan['flags']


def test_placed_beliefs_become_concerns_merged_by_habit_and_lines_name_their_owner(day):
    judge = _judge(("N1", "new", None), ("N2", "same_candidate", "N1"), ("N3", "new", None))
    drs.run_day_pass(day.now, connect=day.plans, register_connect=day.reg.connect, judge=judge)
    [payload] = judge.calls
    assert [c['kind'] for c in payload['candidates']] == ['standing'] * 3
    active = {c['concern_id']: c for c in day.reg.read()['active']}
    assert len(active) == 2, "B1 and B2 are one habit"
    ac = next(c for c in active.values() if 'B1' in c['belief_refs'])
    assert ac['belief_refs'] == ['B1', 'B2'] and ac['kind'] == 'standing'
    assert ac['occurrences'] == {'2026-01-01': [{'time': '9:00 PM', 'belief_keys': ['B1', 'B2']}]}
    owners = {line['belief_key']: line['concern_id'] for line in _lines(day.plans)}
    assert owners['B1'] == owners['B2'] == ac['concern_id'] and owners['B3'] in active
    text = day_plan.render_day(day.now, connect=day.plans)
    assert f"9:00 PM — Set the AC to 70F every night at 9 PM. [belief:B1] [concern:{ac['concern_id']}]" in text


def test_the_next_pass_finds_the_owners_by_code(day, monkeypatch):
    drs.run_day_pass(day.now, connect=day.plans, register_connect=day.reg.connect,
                     judge=_judge(("N1", "new", None), ("N2", "same_candidate", "N1"), ("N3", "new", None)))
    monkeypatch.setattr(drs, '_last_attempt_utc', None)
    monkeypatch.setattr(drs, '_read_daily_context', lambda: {'expected_schedule': [], 'milestones': [{'description': 'changed'}]})
    second = _judge()
    drs.run_day_pass(day.now + timedelta(hours=1), connect=day.plans, register_connect=day.reg.connect, judge=second)
    assert second.calls == [], "no door call when every placed belief already has an owner"
    assert len(day.reg.read()['active']) == 2


def test_a_belief_a_closed_concern_settled_has_no_owner(day):
    day.reg.write({**EMPTY, 'dormant': [{'concern_id': 'c-old', 'title': 'Old AC habit', 'kind': 'standing',
                                         'belief_refs': ['B1', 'B2', 'B3'], 'done_when': 'x', 'evidence': []}]})
    drs.run_day_pass(day.now, connect=day.plans, register_connect=day.reg.connect, judge=_judge())
    assert {line['concern_id'] for line in _lines(day.plans)} == {None}
    assert day.reg.read()['active'] == []


def test_a_belief_folded_into_an_open_matter_concern_is_owned_by_it(day):
    day.reg.write({**EMPTY, 'active': [{'concern_id': 'c-ac', 'title': 'Evening AC', 'kind': 'anticipated_need',
                                        'done_when': 'x', 'evidence': [], 'notes': ''}]})
    drs.run_day_pass(day.now, connect=day.plans, register_connect=day.reg.connect,
                     judge=_judge(("N1", "same_open", "C1"), ("N2", "same_open", "C1"), ("N3", "same_open", "C1")))
    [c] = day.reg.read()['active']
    assert c['belief_refs'] == ['B1', 'B2', 'B3'] and '2026-01-01' in c['occurrences']


def test_record_day_beliefs_saves_only_when_something_changed(tmp_path, monkeypatch):
    reg = ScratchRegister(tmp_path).write({**EMPTY, 'active': [{'concern_id': 'c1', 'title': 't', 'kind': 'standing',
                                                                'belief_refs': ['B1'], 'done_when': 'x'}]})
    saves = []
    real = persist._save_register
    monkeypatch.setattr(persist, '_save_register', lambda connect, register: (saves.append(1), real(connect, register)))
    occ = {'B1': [{'time': '9:00 PM'}]}
    assert persist.record_day_beliefs([], occ, '2026-01-01', connect=reg.connect) == {'B1': 'c1'}
    persist.record_day_beliefs([], occ, '2026-01-01', connect=reg.connect)
    assert saves == [1]


# ── standing concerns are never resolved ──────────────────────────────────────────────────────────────

def test_the_brain_step_may_not_resolve_a_standing_concern():
    from app.assistant.subconscious.brain_step import _problems
    data = {"event_decisions": [{"event": "E1", "decision": "used", "reason": "r"}],
            "concern_updates": [{"concern": "C1", "action": "resolve", "note": "done", "events": ["E1"]}]}
    problems = _problems(data, {"E1": {"text": "x"}}, ["C1"], ["C1"])
    assert any("standing concern" in p for p in problems)
    assert _problems(data, {"E1": {"text": "x"}}, ["C1"], []) == []


def test_the_noticer_may_not_close_a_standing_concern():
    concern = {'concern_id': 'c1', 'kind': 'standing'}
    assert persist._refuse_owner_close(concern, '2026-01-01T00:00:00+00:00', 'resolution (done)') is True
    assert 'REFUSED' in concern['reinforcement_notes']


# ── a standing concern's brief follows what goes wrong, not daily churn ──────────────────────────────

def _standing(**kw):
    return {'concern_id': 'c1', 'title': 'Evening AC', 'kind': 'standing', 'belief_refs': ['B1'], 'done_when': 'x',
            'attached_work': {}, **kw}


def test_daily_churn_does_not_change_a_standing_brief_basis():
    base = concern_brief.basis(_standing(), 'active')
    done = {'w1': {'status': 'done', 'judgments': [{'verdict': 'achieved'}]}}
    churned = _standing(occurrences={'2026-01-01': [{'time': '9:00 PM'}]}, attached_work=done,
                        reinforcement_notes='\n[x] WORK ENDED w1', work_receipts=['r1'])
    assert concern_brief.basis(churned, 'addressing') == base
    failed = _standing(attached_work={'w2': {'status': 'abandoned', 'judgments': [{'verdict': 'unrecoverable'}]}})
    assert concern_brief.basis(failed, 'active') != base
    assert concern_brief.basis(_standing(belief_refs=['B1', 'B9']), 'active') != base


def test_occurrences_do_not_change_a_matter_concern_brief_basis():
    matter = {'concern_id': 'c2', 'title': 'Bake sale', 'kind': 'anticipated_need', 'done_when': 'x'}
    assert concern_brief.basis({**matter, 'occurrences': {'d': []}}, 'active') == concern_brief.basis(matter, 'active')


# ── occurrences are handed over one by one ───────────────────────────────────────────────────────────

def _handoff_register(tmp_path, decision, times):
    day = datetime.now(timezone.utc).astimezone(TZ)
    from app.assistant.pipelines.context import boundary_date_local_str
    key = boundary_date_local_str(day)
    concern = _standing(occurrences={key: [{'time': t, 'belief_keys': ['B1']} for t in times]})
    concern['brief'] = {'what': 'w', 'why_it_matters': 'sleep', 'known': [], 'tried': 'nothing',
                        'owner_wishes': 'nothing said', 'depends_on_it': [], 'open_questions': [],
                        'recommendation': 'set it', 'readiness': {'decision': decision, 'task': 'Set the AC to 70F', 'why': 'nightly'},
                        'basis': concern_brief.basis(concern, 'active')}
    return ScratchRegister(tmp_path).write({**EMPTY, 'active': [concern]}), key


def _handoffs(reg, now, items=()):
    written = []
    concern_handoff.run_handoffs(register_connect=reg.connect, now_utc=now,
                                 existing_items=lambda cid: list(items) + written,
                                 write_item=written.append, poke=lambda: None)
    return written


def test_each_occurrence_is_handed_over_once_with_its_time(tmp_path):
    later = (datetime.now(timezone.utc).astimezone(TZ) + timedelta(hours=2)).strftime('%I:%M %p').lstrip('0')
    reg, key = _handoff_register(tmp_path, 'act_now', [later, 'all day'])
    now = datetime.now(timezone.utc)
    first = _handoffs(reg, now)
    assert [i['summary'] for i in first] == [f'At {later} today ({key}): Set the AC to 70F',
                                             f'All day today ({key}): Set the AC to 70F']
    assert all(i['concern_id'] == 'c1' and i['occurrence_day'] == key for i in first)
    assert _handoffs(reg, now, first) == [], "once per occurrence, even with work in progress"


def test_a_passed_occurrence_and_a_context_concern_are_not_handed_over(tmp_path):
    earlier = (datetime.now(timezone.utc).astimezone(TZ) - timedelta(hours=3)).strftime('%I:%M %p').lstrip('0')
    reg, _ = _handoff_register(tmp_path, 'act_now', [earlier])
    assert _handoffs(reg, datetime.now(timezone.utc)) == []
    later = (datetime.now(timezone.utc).astimezone(TZ) + timedelta(hours=2)).strftime('%I:%M %p').lstrip('0')
    reg, _ = _handoff_register(tmp_path / 'b', 'no_action', [later])
    assert _handoffs(reg, datetime.now(timezone.utc)) == []


def test_a_failed_standing_brief_is_tried_again_the_next_day():
    now = datetime.now(timezone.utc)
    concern = _standing()
    b = concern_brief.basis(concern, 'active')
    today_error = {**concern, 'brief_error': {'basis': b, 'error': 'x', 'at': now.isoformat()}}
    assert concern_brief.stale({'active': [today_error], 'addressing': []}, now) == []
    yesterday = {**concern, 'brief_error': {'basis': b, 'error': 'x', 'at': (now - timedelta(days=1)).isoformat()}}
    assert len(concern_brief.stale({'active': [yesterday], 'addressing': []}, now)) == 1
    matter = {'concern_id': 'c2', 'title': 'Bake sale', 'kind': 'anticipated_need', 'done_when': 'x'}
    old = {**matter, 'brief_error': {'basis': concern_brief.basis(matter, 'active'), 'error': 'x',
                                     'at': (now - timedelta(days=3)).isoformat()}}
    assert concern_brief.stale({'active': [old], 'addressing': []}, now) == [], "a matter's failed brief waits for a change"
