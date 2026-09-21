import json
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
import app.assistant.pipelines.dayflow.steps.dayflow_routine_stage as drs


def entries():
    return [{'belief_key':'lesson.glasses','kind':'stable_preference','tags':['health'],
             'statement':'Often forgets reading glasses for lessons.', 'conditions':{'text':'Before lessons'},
             'last_confirmed':'2026-01-02','status':'active'},
            {'belief_key':'coffee.morning','kind':'episodic_context','tags':[],
             'statement':'Likes creamer in morning coffee.', 'status':'active'}]


def test_projection_preserves_conditions_and_identity():
    out=drs._render_belief_block(entries())
    assert 'lesson.glasses' in out and 'Before lessons' in out
    assert '2026-01-02' in out and 'coffee.morning' in out


def test_llm_selects_cross_domain_belief_without_python_matching(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI
    seen=[]
    def call(msg):
        seen.append(msg.agent_input)
        return SimpleNamespace(data={'belief_ids':['B0'],'reasoning':'Preparation before lesson'})
    monkeypatch.setattr(DI,'agent_factory',SimpleNamespace(create_agent=lambda _:SimpleNamespace(action_handler=call)))
    selected,reason=drs._select_beliefs(entries(),'17:00 instrument lesson',None)
    assert len(seen[0]['belief_catalog'])==2
    assert [e['belief_key'] for e in selected]==['lesson.glasses']
    assert selected[0]['conditions']=={'text':'Before lessons'}


def test_unknown_selection_fails_without_guessing(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI
    monkeypatch.setattr(DI,'agent_factory',SimpleNamespace(create_agent=lambda _:SimpleNamespace(
        action_handler=lambda _:SimpleNamespace(data={'belief_ids':['invented'],'reasoning':'x'}))))
    with pytest.raises(ValueError,match='Unknown selected'): drs._select_beliefs(entries(),'day',None)


@pytest.mark.parametrize('field,value',[
    ('current_status','Glasses already packed'),
    ('milestones',[{'description':'Reminder already delivered'}]),
    ('day_theme','Lesson cancelled'),
])
def test_changed_context_invalidates_projection(field,value):
    old=drs._routine_inputs_fingerprint('2026-01-01',[],'beliefs',daily_context={})
    new=drs._routine_inputs_fingerprint('2026-01-01',[],'beliefs',daily_context={field:value})
    assert old!=new


def test_weekly_changes_invalidate_but_generation_timestamp_does_not():
    make=lambda context,weekly: drs._routine_inputs_fingerprint('day',[],'beliefs',daily_context=context,weekly_insights=weekly)
    assert make({'generated_at':'one'},'a')==make({'generated_at':'two'},'a')
    assert make({},'a')!=make({},'b')


def test_empty_authoritative_calendar_clears_stale_daily_schedule():
    resources={'resource_daily_context_generator_output.json':{'expected_schedule':[{'title':'Cancelled lesson'}]},
               'resource_expected_calendar.json':{'expected_schedule':[]}}
    assert drs.DayFlowRoutineStep()._read_daily_context(SimpleNamespace(read_resource=resources.get))['expected_schedule']==[]


def test_cache_skips_agents_and_milestone_change_regenerates(tmp_path,monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI
    monkeypatch.setattr(DI,'global_blackboard',None)
    monkeypatch.setattr(drs,'_read_belief_entries',entries)
    monkeypatch.setattr(drs,'_format_weekly_insights',lambda:'')
    monkeypatch.setattr(drs,'_format_daily_context',lambda data,**kwargs:(json.dumps(data),''))
    monkeypatch.setattr(drs,'load_scope_for_source',lambda **kwargs:None)
    calls=[]
    monkeypatch.setattr(drs,'_select_beliefs',lambda *args,**kwargs:(calls.append('select') or entries(),'relevant'))
    step=drs.DayFlowRoutineStep()
    monkeypatch.setattr(step,'_call_agent',lambda **kwargs:(calls.append('write') or '### 16:00\nGlasses guidance','changed'))
    resources={'resource_daily_context_generator_output.json':{'expected_schedule':[],'milestones':[]}}
    now=datetime(2026,1,1,15,tzinfo=timezone.utc)
    ctx=SimpleNamespace(now_utc=now,now_local=now,state={},resources_dir=tmp_path,
        read_resource=resources.get,write_resource=lambda k,v:resources.__setitem__(k,v),
        day_archive_dir=lambda _:tmp_path/'archive')
    step.run(ctx); step.run(ctx)
    assert calls==['select','write']
    resources['resource_daily_context_generator_output.json']['milestones']=[{'description':'Glasses packed'}]
    step.run(ctx)
    assert calls==['select','write','select','write']


def test_catalog_metadata_change_invalidates_selection():
    before = entries()
    after = entries()
    after[0]['tags'] = ['travel']
    make = lambda catalog: drs._routine_inputs_fingerprint('day', [], 'same rendering', belief_catalog=catalog)
    assert make(before) != make(after)


def test_weekly_candidates_do_not_bypass_belief_reconciliation(tmp_path, monkeypatch):
    path = tmp_path / 'weekly.json'
    path.write_text(json.dumps({'week_start': '2026-01-01', 'week_end': '2026-01-07',
        'insights': {'belief_candidates': [{'statement': 'Coffee preference', 'domain': 'food',
            'confidence': 'medium', 'conditions': 'Morning only', 'evidence': ['source-1']}]*4}}), encoding='utf-8')
    monkeypatch.setattr(drs, '_WEEKLY_INSIGHTS_PATH', path)
    text = drs._format_weekly_insights()
    assert 'Coffee preference' not in text
    assert 'reconciled through the belief engine' in text

@pytest.fixture(autouse=True)
def isolated_selection_services(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    monkeypatch.setattr(ServiceLocator, '_services', {'agent_factory': SimpleNamespace(), 'global_blackboard': SimpleNamespace()})

def test_empty_writer_is_failure_not_success(monkeypatch, tmp_path):
    from app.assistant.ServiceLocator.service_locator import DI
    monkeypatch.setattr(drs, '_read_belief_entries', entries)
    monkeypatch.setattr(drs, '_format_weekly_insights', lambda: '')
    monkeypatch.setattr(drs, '_format_daily_context', lambda *a, **k: ('context',''))
    monkeypatch.setattr(drs, 'load_scope_for_source', lambda **k: None)
    monkeypatch.setattr(drs, '_select_beliefs', lambda *a, **k: (entries(),'relevant'))
    step=drs.DayFlowRoutineStep()
    monkeypatch.setattr(step, '_read_daily_context', lambda ctx: {})
    monkeypatch.setattr(step, '_call_agent', lambda **k: (None,''))
    now=datetime(2026,1,1,15,tzinfo=timezone.utc)
    ctx=SimpleNamespace(now_utc=now,now_local=now,state={},resources_dir=tmp_path,
                        read_resource=lambda key: None)
    with pytest.raises(RuntimeError, match='guidance was not refreshed'):
        step.run(ctx)
    assert ctx.state == {}
