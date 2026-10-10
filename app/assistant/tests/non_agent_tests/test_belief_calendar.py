"""Beliefs drive work through today's calendar (docs/design/what_was_done_reaches_the_brain_2026-10-03.md).

Pins: only the beliefs the owner approved for the calendar are placed, each read with the beliefs closest to
it in meaning (found by code over the stored embeddings, nearest first, up to RELATED_MAX at RELATED_MIN or
closer); the writer is shown them and places only approved beliefs; the plan line keeps them. Until 2026-10-10 each
placement was also handed to dayflow's steward as a belief intake item (belief_occurrences, retired with the steward;
the day's placements are concerns, test_calendar_concerns.py). The standing concerns of 2026-10-02/03
retire to dormant and the concern door no longer compares candidates with them. Scratch register and plan
store; every agent is a fake. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.assistant.tests.test_setup  # noqa: F401
import app.assistant.subconscious.day_pass as drs
from app.assistant.subconscious import concern_door, day_plan
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
        {'belief_key': 'B1', 'why': 'Wednesday.', 'applies_today': True, 'outcome': 'B1 done',
         'times': [{'time': '9:00 PM', 'until': '11:00 PM'}]},
        {'belief_key': 'B3', 'why': 'Every day.', 'applies_today': True, 'outcome': 'B3 done',
         'times': [{'time': '2:00 PM', 'until': '4:00 PM'}]}]})[1])
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
        {'belief_key': 'B1', 'why': 'Not Wednesday.', 'applies_today': False, 'outcome': '', 'times': []},
        {'belief_key': 'B2', 'why': 'A holiday.', 'applies_today': True, 'outcome': 'B2 done',
         'times': [{'time': '9:00 PM', 'until': '11:00 PM'}]}]}, {'B1'}, {'B2'})
    assert problems == ["'B2' is read beside the approved beliefs, not an entry: leave it out"]


def test_every_approved_belief_is_judged_once_and_only_those_that_apply_are_placed():
    """2026-10-05: the writer listed beliefs its own reason said did not apply; now each is judged, reason first."""
    answer = {'judgments': [
        {'belief_key': 'B1', 'why': 'Business day 3, not the first.', 'applies_today': False, 'outcome': '',
         'times': [{'time': 'all day', 'until': ''}]},
        {'belief_key': 'B3', 'why': 'Every day.', 'applies_today': True, 'outcome': 'B3 done',
         'times': [{'time': '10:00 AM', 'until': '11:00 AM'}, {'time': '3:00 PM', 'until': '5:00 PM'}]}]}
    assert drs._problems(answer, {'B1', 'B3'}) == []
    assert drs._placements(answer) == [
        {'belief_key': 'B3', 'time': '10:00 AM', 'until': '11:00 AM', 'outcome': 'B3 done'},
        {'belief_key': 'B3', 'time': '3:00 PM', 'until': '5:00 PM', 'outcome': 'B3 done'}]
    assert drs._problems({'judgments': answer['judgments'][1:]}, {'B1', 'B3'}) == \
        ['B1: no judgment; judge every approved belief']
    assert drs._problems({'judgments': [*answer['judgments'], answer['judgments'][0]]}, {'B1', 'B3'}) == \
        ['B1: judge each approved belief once']
    assert drs._problems({'judgments': [answer['judgments'][0], {**answer['judgments'][1], 'times': []}]},
                         {'B1', 'B3'}) == ["B3: it applies today, so give the time it applies, or 'all day'"]


# ── beliefs are not concerns ──────────────────────────────────────────────────────────────────────────

def test_the_door_does_not_compare_candidates_with_retired_standing_concerns():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    register = {**EMPTY, 'dormant': [{'concern_id': 'c1', 'kind': 'standing'}, {'concern_id': 'c2', 'chronic': True}]}
    assert [c['concern_id'] for c in concern_door._comparable(register, now)] == ['c2']


# ── a placement is decided once (2026-10-10) ─────────────────────────────────────────────────────────────

def test_a_belief_placed_earlier_today_keeps_its_placement_until_the_schedule_or_the_belief_changes(day, monkeypatch):
    """2026-10-10: every rewrite judged every belief afresh, and the lights-off-after-bedtime belief flipped in and
    out of the day, its concern withdrawn and its work ended each time."""
    drs.run_day_pass(day.now, connect=day.plans)
    shown = []
    monkeypatch.setattr(drs, '_write', lambda agent_input: (shown.append(agent_input['beliefs_block']), {'judgments': [
        {'belief_key': k, 'why': 'Not today.', 'applies_today': False, 'outcome': '', 'times': []}
        for k in ('B1', 'B3') if f'[belief:{k}]' in agent_input['beliefs_block']]})[1])
    monkeypatch.setattr(drs, '_read_daily_context', lambda: {'expected_schedule': [], 'milestones': [{'time': '12:30 PM', 'description': 'Lunch eaten'}]})
    monkeypatch.setattr(drs, '_last_attempt_utc', None)
    later = day.now + timedelta(hours=2)
    drs.run_day_pass(later, connect=day.plans)
    assert not shown, "nothing changed for either belief: neither is judged again"
    assert sorted(line['belief_key'] for line in _lines(day.plans)) == ['B1', 'B3']

    trash_night = {'title': 'Trash night', 'start_local': '08:30 PM', 'end_local': '09:00 PM', 'status': 'upcoming',
                   'start_utc': (later + timedelta(hours=5)).isoformat(),
                   'end_utc': (later + timedelta(hours=5, minutes=30)).isoformat()}
    monkeypatch.setattr(drs, '_read_daily_context', lambda: {'expected_schedule': [trash_night], 'milestones': []})
    monkeypatch.setattr(drs, '_last_attempt_utc', None)
    drs.run_day_pass(later + timedelta(hours=1, minutes=1), connect=day.plans)
    assert shown and '[belief:B1]' in shown[-1], "the schedule ahead changed: the beliefs are judged again"
    assert _lines(day.plans) == []
