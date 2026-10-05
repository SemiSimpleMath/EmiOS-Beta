"""Beliefs drive work through today's calendar (docs/design/what_was_done_reaches_the_brain_2026-10-03.md).

Pins: only the beliefs the owner approved for the calendar are placed, each read with the beliefs closest to
it in meaning (found by code over the stored embeddings, nearest first, up to RELATED_MAX at RELATED_MIN or
closer); the writer is shown them and places only approved beliefs; the plan line keeps them. Each placement is handed to dayflow as a belief intake item once, not when an hour
past; the work made from it carries the belief. Beliefs are not made into concerns: the standing concerns
retire to dormant and the concern door no longer compares candidates with them. Scratch register and plan
store; every agent is a fake. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.assistant.tests.test_setup  # noqa: F401
import app.assistant.subconscious.day_pass as drs
from app.assistant.subconscious import belief_occurrences, concern_brief, concern_door, day_plan, persist
from app.assistant.tests.concern_store_helpers import ScratchRegister
from app.assistant.utils.time_utils import get_local_timezone
from belief_engine.intake.store import sqlite_file

TZ = get_local_timezone()
EMPTY = {"active": [], "addressing": [], "resolved": [], "dormant": []}
BELIEFS = [{'belief_key': 'B1', 'statement': 'Takes the trash out on Wednesday nights.', 'status': 'active',
            'calendar': 'approved'},
           {'belief_key': 'B2', 'statement': 'After a Monday holiday, trash night moves to Thursday.', 'status': 'active',
            'calendar': 'not_proposed'},
           {'belief_key': 'B3', 'statement': 'Drinks coffee at 10 AM.', 'status': 'active', 'calendar': 'approved'},
           {'belief_key': 'B4', 'statement': 'Likes potato balls.', 'status': 'active', 'calendar': 'declined'}]
# B1 and B2 point the same way, B3 elsewhere, B4 nowhere near either.
VECTORS = {'B1': [1.0, 0.0, 0.0], 'B2': [0.9, 0.3, 0.0], 'B3': [0.0, 1.0, 0.0], 'B4': [0.0, 0.0, 1.0]}


@pytest.fixture
def day(monkeypatch, tmp_path):
    import app.assistant.scope.loader as loader
    monkeypatch.setattr(drs, '_read_daily_context', lambda: {'expected_schedule': [], 'milestones': []})
    monkeypatch.setattr(drs, '_read_belief_entries', lambda: [dict(b) for b in BELIEFS])
    monkeypatch.setattr(drs, '_belief_vectors', lambda: dict(VECTORS))
    monkeypatch.setattr(loader, 'load_scope_for_source', lambda **k: None)
    shown = []
    monkeypatch.setattr(drs, '_write', lambda agent_input: (shown.append(agent_input), {'judgments': [
        {'belief_key': 'B1', 'why': 'Wednesday.', 'applies_today': True, 'times': ['9:00 PM']},
        {'belief_key': 'B3', 'why': 'Every day.', 'applies_today': True, 'times': ['2:00 PM']}]})[1])
    monkeypatch.setattr(drs, '_last_attempt_utc', None)
    return SimpleNamespace(plans=sqlite_file(tmp_path / 'plan.db'), shown=shown,
                           now=datetime(2026, 1, 7, 13, 0, tzinfo=TZ).astimezone(timezone.utc))


def _lines(plans, key='2026-01-07'):
    plan = day_plan.load_plan(key, connect=plans)
    return [line for s in plan['slots'] for line in s['lines']] + plan['flags']


# ── approved beliefs and their related ones ──────────────────────────────────────────────────────────────

def test_only_approved_beliefs_are_placed_each_with_its_related_ones(day):
    drs.run_day_pass(day.now, connect=day.plans)
    [shown] = day.shown
    assert '[belief:B1] (topic' not in shown['beliefs_block']
    assert 'Takes the trash out on Wednesday nights.' in shown['beliefs_block']
    assert 'Likes potato balls' not in shown['beliefs_block'], "a declined belief is not offered"
    assert '- [belief:B2] After a Monday holiday, trash night moves to Thursday.' in shown['beliefs_block']
    trash = next(line for line in _lines(day.plans) if line['belief_key'] == 'B1')
    assert trash['related'] == [{'belief_key': 'B2', 'statement': BELIEFS[1]['statement']}]
    plan = day_plan.load_plan('2026-01-07', connect=day.plans)
    assert plan['selected_belief_keys'] == ['B1', 'B3'] and plan['qualifier_belief_keys'] == ['B2']


def test_related_beliefs_are_the_nearest_close_enough(monkeypatch):
    monkeypatch.setattr(drs, 'RELATED_MAX', 1)
    vectors = {**VECTORS, 'B5': [0.95, 0.1, 0.0]}
    entries = [*BELIEFS, {'belief_key': 'B5', 'statement': 'Pickup is on Thursdays.', 'status': 'active'}]
    related = drs._related([BELIEFS[0], BELIEFS[3]], entries, vectors)
    assert [r['belief_key'] for r in related['B1']] == ['B5'], "nearest first, at most RELATED_MAX"
    assert related['B4'] == [], "nothing near enough"


def test_a_related_belief_is_never_placed_as_an_entry():
    problems = drs._problems({'judgments': [
        {'belief_key': 'B1', 'why': 'Not Wednesday.', 'applies_today': False, 'times': []},
        {'belief_key': 'B2', 'why': 'A holiday.', 'applies_today': True, 'times': ['9:00 PM']}]}, {'B1'}, {'B2'})
    assert problems == ["'B2' is read beside the approved beliefs, not an entry: leave it out"]


def test_every_approved_belief_is_judged_once_and_only_those_that_apply_are_placed():
    """2026-10-05: the writer listed beliefs its own reason said did not apply; now each is judged, reason first."""
    answer = {'judgments': [
        {'belief_key': 'B1', 'why': 'Business day 3, not the first.', 'applies_today': False, 'times': ['all day']},
        {'belief_key': 'B3', 'why': 'Every day.', 'applies_today': True, 'times': ['10:00 AM', '3:00 PM']}]}
    assert drs._problems(answer, {'B1', 'B3'}) == []
    assert drs._placements(answer) == [{'belief_key': 'B3', 'time': '10:00 AM'}, {'belief_key': 'B3', 'time': '3:00 PM'}]
    assert drs._problems({'judgments': answer['judgments'][1:]}, {'B1', 'B3'}) == \
        ['B1: no judgment; judge every approved belief']
    assert drs._problems({'judgments': [*answer['judgments'], answer['judgments'][0]]}, {'B1', 'B3'}) == \
        ['B1: judge each approved belief once']
    assert drs._problems({'judgments': [answer['judgments'][0], {**answer['judgments'][1], 'times': []}]},
                         {'B1', 'B3'}) == ["B3: it applies today, so give the time it applies, or 'all day'"]


# ── each placement is handed to dayflow ───────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _lanes_off(monkeypatch):
    """The steward's path: both lanes off, whatever configs/brain_lanes.json says today."""
    from app.assistant.dayflow_orchestrator import brain_list_intake
    monkeypatch.setattr(brain_list_intake, "lanes", lambda: {"calendar": {"mode": "off", "from_day": None},
                                                             "concern": {"mode": "off", "since": None}})


NOON = datetime(2026, 1, 7, 12, 0, tzinfo=TZ).astimezone(timezone.utc)


def _clock(dt):
    return dt.astimezone(TZ).strftime('%I:%M %p').lstrip('0')


def _plan_today(tmp_path, times):
    from app.assistant.pipelines.context import boundary_date_local_str
    key = boundary_date_local_str(NOON.astimezone(TZ))
    plans = sqlite_file(tmp_path / 'plan.db')
    line = lambda t: {'time': t, 'belief_key': 'B1', 'statement': 'Takes the trash out on Wednesday nights.',  # noqa: E731
                      'related': [{'belief_key': 'B2', 'statement': 'After a holiday, Thursday.'}]}
    day_plan.save_plan({'day': key, 'generated_at_utc': 'x', 'inputs_fingerprint': 'f', 'selected_belief_keys': ['B1'],
                        'qualifier_belief_keys': ['B2'], 'selection_reason': '', 'change_summary': '', 'backdrops': [],
                        'flags': [line(t) for t in times if t == 'all day'],
                        'slots': [{'slot': 's', 'hour_local_iso': f'{key}T12:00:00', 'items': [],
                                   'lines': [line(t) for t in times if t != 'all day']}]}, connect=plans)
    return plans, key


def _hand(plans, now, items=()):
    written = []
    belief_occurrences.run_occurrence_handoffs(now_utc=now, plan_connect=plans,
                                               handed_ids=lambda day: {i['item_id'] for i in [*items, *written]},
                                               write_item=written.append, poke=lambda: None)
    return written


def test_each_placement_is_handed_over_once_with_its_time_and_related_beliefs(tmp_path):
    later = _clock(NOON + timedelta(hours=2))
    plans, key = _plan_today(tmp_path, [later, 'all day'])
    first = _hand(plans, NOON)
    assert sorted(i['summary'] for i in first) == sorted([
        f'At {later} today ({key}): Takes the trash out on Wednesday nights.',
        f'All day today ({key}): Takes the trash out on Wednesday nights.'])
    assert all(i['source_type'] == 'belief' and i['belief_key'] == 'B1' and i['occurrence_day'] == key
               and i['read_with'] == '- After a holiday, Thursday.' and i['evaluator_pending']
               for i in first)
    assert _hand(plans, NOON, first) == [], "once per placement"


def test_a_placement_more_than_an_hour_past_is_not_handed_over(tmp_path):
    plans, _ = _plan_today(tmp_path, [_clock(NOON - timedelta(hours=3)), _clock(NOON - timedelta(minutes=30))])
    assert [i['occurrence_time'] for i in _hand(plans, NOON)] == ['11:30 AM']


def test_a_placement_after_midnight_belongs_to_the_end_of_the_day():
    from app.assistant.pipelines.context import day_window_local
    start, _ = day_window_local('2026-10-02')
    before = (start + timedelta(hours=19)).astimezone(timezone.utc)          # 12:00 AM the next calendar date
    assert not belief_occurrences.passed('2026-10-02', '12:30 AM', before)
    assert belief_occurrences.passed('2026-10-02', '12:30 AM', before + timedelta(hours=2))
    assert belief_occurrences.passed('2026-10-02', '9:00 PM', before)


def test_work_made_from_a_placement_carries_the_belief():
    from app.assistant.dayflow_orchestrator.work_intake import belief_refs_of
    assert belief_refs_of([{'item_id': 'a', 'belief_key': 'B1'}, {'item_id': 'b', 'concern_id': 'c'}]) == ['B1']


# ── beliefs are not concerns ──────────────────────────────────────────────────────────────────────────

def test_standing_concerns_retire_to_dormant(tmp_path):
    reg = ScratchRegister(tmp_path).write({**EMPTY, 'active': [
        {'concern_id': 'c1', 'title': 'Evening AC', 'kind': 'standing', 'occurrences': {'d': []}},
        {'concern_id': 'c2', 'title': 'Bake sale', 'kind': 'anticipated_need'}]})
    assert persist.retire_standing_concerns('beliefs are not concerns', connect=reg.connect) == ['c1']
    after = reg.read()
    assert [c['concern_id'] for c in after['active']] == ['c2']
    [retired] = after['dormant']
    assert retired['dormant_reason'] == 'beliefs are not concerns' and 'occurrences' not in retired
    assert 'RETIRED to dormant' in retired['reinforcement_notes']
    assert persist.retire_standing_concerns('again', connect=reg.connect) == []


def test_the_door_does_not_compare_candidates_with_retired_standing_concerns():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    register = {**EMPTY, 'dormant': [{'concern_id': 'c1', 'kind': 'standing'}, {'concern_id': 'c2', 'chronic': True}]}
    assert [c['concern_id'] for c in concern_door._comparable(register, now)] == ['c2']


def test_a_brief_is_written_again_only_when_the_record_changes():
    matter = {'concern_id': 'c2', 'title': 'Bake sale', 'kind': 'anticipated_need', 'done_when': 'x'}
    b = concern_brief.basis(matter, 'active')
    assert concern_brief.basis({**matter, 'brief': {'what': 'w'}, 'plan': {'decision': 'hold'}}, 'active') == b
    briefed = {**matter, 'brief': {'basis': b}}
    assert concern_brief.stale({'active': [briefed], 'addressing': []}) == []
    failed = {**matter, 'brief_error': {'basis': b, 'writer': concern_brief.WRITER_REVISION}}
    assert concern_brief.stale({'active': [failed], 'addressing': []}) == [], "a failed brief waits for a change"
    assert len(concern_brief.stale({'active': [{**matter, 'notes': 'new'}], 'addressing': []})) == 1
