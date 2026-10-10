import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
import app.assistant.subconscious.day_pass as drs
from app.assistant.subconscious import day_plan
from belief_engine.intake.store import sqlite_file


def entries():
    return [{'belief_key':'lesson.glasses','kind':'stable_preference','tags':['health'],
             'statement':'Often forgets reading glasses for lessons.', 'conditions':{'text':'Before lessons'},
             'last_confirmed':'2026-01-02','status':'active','calendar':'approved'},
            {'belief_key':'coffee.morning','kind':'episodic_context','tags':[],
             'statement':'Likes creamer in morning coffee.', 'status':'active','calendar':'approved'}]


@pytest.fixture
def connect(tmp_path):
    return sqlite_file(tmp_path / 'plan.db')


@pytest.fixture(autouse=True)
def isolated_selection_services(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    monkeypatch.setattr(ServiceLocator, '_services', {'agent_factory': SimpleNamespace(), 'global_blackboard': SimpleNamespace()})
    monkeypatch.setattr(drs, '_last_attempt_utc', None)


def test_projection_preserves_conditions_and_identity():
    out=drs._render_belief_block(entries())
    assert 'lesson.glasses' in out and 'Before lessons' in out
    assert '2026-01-02' in out and 'coffee.morning' in out


@pytest.mark.parametrize('field,value',[
    ('current_status','Glasses already packed'),
    ('milestones',[{'description':'Reminder already delivered'}]),
])
def test_changed_context_invalidates_projection(field,value):
    old=drs._routine_inputs_fingerprint('2026-01-01',[],'beliefs',daily_context={})
    new=drs._routine_inputs_fingerprint('2026-01-01',[],'beliefs',daily_context={field:value})
    assert old!=new


def test_empty_authoritative_calendar_clears_stale_daily_schedule(tmp_path, monkeypatch):
    (tmp_path/'resource_daily_context_generator_output.json').write_text(
        json.dumps({'expected_schedule':[{'title':'Cancelled lesson'}]}), encoding='utf-8')
    (tmp_path/'resource_expected_calendar.json').write_text(json.dumps({'expected_schedule':[]}), encoding='utf-8')
    monkeypatch.setattr(drs, '_INPUTS_DIR', tmp_path)
    assert drs._read_daily_context()['expected_schedule']==[]


# ── the day pass: placements, caching, validation ─────────────────────────────────────────────────────

UNTIL = '11:30 PM'


def _judged(placements):
    """The writer's answer for these placements: every approved belief judged, applying at its placed times, each
    clock time mattering until UNTIL; a placement of any other key is judged as applying."""
    keys = ['lesson.glasses', 'coffee.morning'] + [p['belief_key'] for p in placements
                                                   if p['belief_key'] not in ('lesson.glasses', 'coffee.morning')]
    judgments = []
    for key in dict.fromkeys(keys):
        times = [{'time': p['time'], 'until': '' if p['time'] == 'all day' else UNTIL}
                 for p in placements if p['belief_key'] == key]
        judgments.append({'belief_key': key, 'why': 'test', 'applies_today': bool(times),
                          'outcome': f'{key} is done' if times else '', 'times': times})
    return {'judgments': judgments}


def _writer_for(calls, placements=None):
    """A writer that places the glasses belief before a lesson and the coffee belief all day."""
    def write(agent_input):
        calls.append('write')
        return _judged(placements if placements is not None else [
            {'belief_key': 'lesson.glasses', 'time': '4:30 PM'}, {'belief_key': 'coffee.morning', 'time': 'all day'}])
    return write


@pytest.fixture
def day_inputs(monkeypatch):
    """A day with one lesson; returns the daily context the day pass reads, and the call log."""
    import app.assistant.scope.loader as loader
    calls, context = [], {'expected_schedule': [], 'milestones': []}
    monkeypatch.setattr(drs, '_read_daily_context', lambda: json.loads(json.dumps(context)))
    monkeypatch.setattr(drs, '_read_belief_entries', entries)
    monkeypatch.setattr(loader, 'load_scope_for_source', lambda **k: None)
    monkeypatch.setattr(drs, '_write', _writer_for(calls))
    # Related beliefs are pinned in test_belief_calendar.py; these two point different ways and relate to nothing.
    monkeypatch.setattr(drs, '_belief_vectors', lambda: {'lesson.glasses': [1.0, 0.0], 'coffee.morning': [0.0, 1.0]})
    return context, calls


def test_the_plan_is_written_once_and_regenerated_hourly_on_change(connect, day_inputs):
    context, calls = day_inputs
    now = datetime(2026, 1, 1, 18, tzinfo=timezone.utc)
    assert drs.run_day_pass(now, connect=connect)['day_pass'] == 'written'
    assert drs.run_day_pass(now, connect=connect) == {'day_pass': 'unchanged'}
    assert calls == ['write']
    context['milestones'] = [{'description': 'Glasses packed'}]
    assert drs.run_day_pass(now + timedelta(minutes=20), connect=connect) == {'day_pass': 'changed_too_soon'}
    assert drs.run_day_pass(now + timedelta(hours=1), connect=connect)['day_pass'] == 'written'
    # Every belief the first plan placed is unchanged and so is the schedule ahead: its placement stands, and the
    # writer is not asked again (a placement is decided once, 2026-10-10).
    assert calls == ['write']


def _lesson_day(context):
    from app.assistant.utils.time_utils import get_local_timezone
    tz = get_local_timezone()
    start = datetime(2026, 1, 1, 17, 0, tzinfo=tz)      # a 5 PM lesson, local
    context['expected_schedule'] = [{'title': 'Lesson', 'start_local': '5:00 PM', 'end_local': '6:00 PM',
                                     'start_utc': start.astimezone(timezone.utc).isoformat(),
                                     'end_utc': (start + timedelta(hours=1)).astimezone(timezone.utc).isoformat()}]
    return tz


def test_the_writer_places_beliefs_and_code_builds_the_day(connect, day_inputs):
    context, _ = day_inputs
    tz = _lesson_day(context)
    now = datetime(2026, 1, 1, 13, 30, tzinfo=tz).astimezone(timezone.utc)
    drs.run_day_pass(now, connect=connect)
    plan = day_plan.load_plan('2026-01-01', connect=connect)
    by_slot = {s['slot']: s for s in plan['slots']}
    assert by_slot['2026-01-01T16:00']['lines'] == [
        {'time': '4:30 PM', 'belief_key': 'lesson.glasses', 'statement': 'Often forgets reading glasses for lessons.',
         'related': [], 'outcome': 'lesson.glasses is done', 'until': UNTIL}]
    assert by_slot['2026-01-01T17:00']['items'][0]['title'] == 'Lesson'
    assert plan['flags'] == [{'time': 'all day', 'belief_key': 'coffee.morning',
                              'statement': 'Likes creamer in morning coffee.', 'related': [],
                              'outcome': 'coffee.morning is done', 'until': ''}]

    text = day_plan.render_day(now, connect=connect)
    assert '### Now (1:30 PM)' in text
    assert '### 2:00 PM – 4:00 PM — open' in text            # empty hours merged at render time
    assert '### 4:00 PM\n- 4:30 PM — Often forgets reading glasses for lessons. [belief:lesson.glasses]' in text
    assert '### 5:00 PM — Lesson (5:00 PM–6:00 PM)' in text
    assert text.rstrip().endswith('## All day\n- Likes creamer in morning coffee. [belief:coffee.morning]')
    later = day_plan.render_day(now + timedelta(hours=3), connect=connect)   # 4:30 PM: the past is not shown
    assert '1:30 PM' not in later and '2:00 PM' not in later and '### Now (4:30 PM)' in later


def test_a_9_pm_belief_is_filed_at_9_pm_and_a_belief_can_have_two_times(connect, day_inputs, monkeypatch):
    context, _ = day_inputs
    tz = _lesson_day(context)
    monkeypatch.setattr(drs, '_write', _writer_for([], [
        {'belief_key': 'lesson.glasses', 'time': '9:00 PM'}, {'belief_key': 'lesson.glasses', 'time': '9:00 PM'},
        {'belief_key': 'coffee.morning', 'time': '9:15 pm'}, {'belief_key': 'coffee.morning', 'time': '7:00 AM'}]))
    now = datetime(2026, 1, 1, 8, 30, tzinfo=tz).astimezone(timezone.utc)
    drs.run_day_pass(now, connect=connect)
    by_slot = {s['slot']: s['lines'] for s in day_plan.load_plan('2026-01-01', connect=connect)['slots']}
    assert [(l['belief_key'], l['time']) for l in by_slot['2026-01-01T21:00']] == [
        ('lesson.glasses', '9:00 PM'), ('coffee.morning', '9:15 pm')]     # time order, the repeat stored once
    assert by_slot['2026-01-01T09:00'] == []
    # The grid starts an hour before now (7:00 AM at 8:30 AM), so a 7:00 AM placement is still shown.
    assert [(l['belief_key'], l['time']) for l in by_slot['2026-01-01T07:00']] == [('coffee.morning', '7:00 AM')]


def test_a_placement_before_the_grid_has_passed_and_is_not_stored(connect, day_inputs, monkeypatch):
    context, _ = day_inputs
    tz = _lesson_day(context)
    monkeypatch.setattr(drs, '_write', _writer_for([], [{'belief_key': 'coffee.morning', 'time': '7:00 AM'}]))
    drs.run_day_pass(datetime(2026, 1, 1, 13, 30, tzinfo=tz).astimezone(timezone.utc), connect=connect)
    plan = day_plan.load_plan('2026-01-01', connect=connect)
    assert all(not s['lines'] for s in plan['slots']) and plan['flags'] == []


def test_an_invalid_answer_gets_one_correction(connect, day_inputs, monkeypatch):
    seen = []
    answers = iter([_judged([{'belief_key': 'lesson.glasses', 'time': 'evening'}]),
                    _judged([{'belief_key': 'lesson.glasses', 'time': '6:00 PM'}])])

    def write(agent_input):
        seen.append(agent_input.get('correction'))
        return next(answers)
    monkeypatch.setattr(drs, '_write', write)
    drs.run_day_pass(datetime(2026, 1, 1, 18, tzinfo=timezone.utc), connect=connect)
    assert seen[0] is None and "'evening' is not a clock time" in seen[1]
    assert day_plan.load_plan('2026-01-01', connect=connect) is not None


@pytest.mark.parametrize('placement,error', [
    ({'belief_key': 'invented.key', 'time': '6:00 PM'}, 'not one of the beliefs given'),
    ({'belief_key': 'lesson.glasses', 'time': 'soon'}, 'is not a clock time'),
])
def test_a_still_invalid_answer_is_refused(connect, day_inputs, monkeypatch, placement, error):
    monkeypatch.setattr(drs, '_write', _writer_for([], [placement]))
    with pytest.raises(ValueError, match=error):
        drs.run_day_pass(datetime(2026, 1, 1, 18, tzinfo=timezone.utc), connect=connect)
    assert day_plan.load_plan('2026-01-01', connect=connect) is None


def test_an_applying_belief_needs_its_outcome_and_a_clock_time_its_until(connect, day_inputs, monkeypatch):
    """Rule 7: each placement becomes a concern, which needs what it asks for today and until when it matters."""
    bad = {'judgments': [{'belief_key': 'lesson.glasses', 'why': 'w', 'applies_today': True, 'outcome': '',
                          'times': [{'time': '6:00 PM', 'until': 'later'}]},
                         {'belief_key': 'coffee.morning', 'why': 'w', 'applies_today': False, 'outcome': '', 'times': []}]}
    monkeypatch.setattr(drs, '_write', lambda agent_input: bad)
    with pytest.raises(ValueError) as raised:
        drs.run_day_pass(datetime(2026, 1, 1, 18, tzinfo=timezone.utc), connect=connect)
    assert "say what it asks for today" in str(raised.value) and "until 'later' is not a clock time" in str(raised.value)


def test_no_plan_for_today_says_so(connect):
    assert 'No plan of the day has been written' in day_plan.render_day(
        datetime(2026, 1, 2, 18, tzinfo=timezone.utc), connect=connect)


def test_the_writer_is_shown_the_schedule_as_a_list(connect, day_inputs, monkeypatch):
    context, _ = day_inputs
    tz = _lesson_day(context)
    seen = []

    def write(agent_input):
        seen.append(agent_input)
        return _judged([])
    monkeypatch.setattr(drs, '_write', write)
    context['day_theme'] = 'Monthly timesheets are due on this first business day.'
    drs.run_day_pass(datetime(2026, 1, 1, 13, 30, tzinfo=tz).astimezone(timezone.utc), connect=connect)
    assert "Today's schedule:\n  Lesson 5:00 PM - 6:00 PM" in seen[0]['daily_context']
    assert 'Monthly timesheets' not in seen[0]['daily_context'], "the tracker's summary is not a fact for the writer"
    assert seen[0]['day_facts']['business_day_number'] == 1 and seen[0]['day_facts']['weekday'] == 'Thursday'
    assert '[slot' not in seen[0]['daily_context'] and '(open)' not in seen[0]['daily_context']


def test_slot_keys_stay_unique_across_midnight():
    from app.assistant.pipelines.dayflow.utils.expected_calendar_utils import build_hour_grid
    from app.assistant.utils.time_utils import get_local_timezone
    tz = get_local_timezone()
    now = datetime(2026, 1, 2, 0, 30, tzinfo=tz).astimezone(timezone.utc)
    slots = build_hour_grid([], now, (datetime(2026, 1, 3, 0, 0, tzinfo=tz)).astimezone(timezone.utc))
    labels = [s['label'] for s in slots]
    keys = [s['slot'] for s in slots]
    assert labels.count('11:00 PM') == 2 and len(set(keys)) == len(keys)


def test_the_writer_judges_the_approved_beliefs_in_batches(connect, day_inputs, monkeypatch):
    """2026-10-05: one call judging all 45 left a belief out in 2 of 2 live answers; at most BATCH per call."""
    many = [{'belief_key': f'b{n:02d}', 'statement': f'Belief {n}.', 'status': 'active', 'calendar': 'approved'}
            for n in range(23)]
    monkeypatch.setattr(drs, '_read_belief_entries', lambda: many)
    monkeypatch.setattr(drs, '_belief_vectors', lambda: {e['belief_key']: [1.0, float(n)] for n, e in enumerate(many)})
    monkeypatch.setattr(drs, 'RELATED_MIN', 2.0)       # nothing related: each call shows only its own batch
    asked = []

    def write(agent_input):
        keys = [line.split(']')[0].removeprefix('- [belief:') for line in agent_input['beliefs_block'].splitlines()
                if line.startswith('- [belief:')]
        asked.append(keys)
        return {'judgments': [{'belief_key': k, 'why': 'test', 'applies_today': k == 'b07', 'outcome': 'done',
                               'times': [{'time': 'all day', 'until': ''}]} for k in keys]}
    monkeypatch.setattr(drs, '_write', write)
    drs.run_day_pass(datetime(2026, 1, 1, 18, tzinfo=timezone.utc), connect=connect)
    assert [len(a) for a in asked] == [10, 10, 3] and sorted(sum(asked, [])) == [e['belief_key'] for e in many]
    assert [f['belief_key'] for f in day_plan.load_plan('2026-01-01', connect=connect)['flags']] == ['b07']

