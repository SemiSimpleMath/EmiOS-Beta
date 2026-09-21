from types import SimpleNamespace
from dataclasses import asdict
import pytest
from belief_engine.pipeline.steps import update_beliefs as U
from belief_engine.pipeline.steps.collect_evidence import EvidenceBundle,EvidenceItem

def item(day,ref,text='source'):
    return EvidenceItem('daily_insights',day,ref,'confirms',text,text,1)

def test_overflow_splits_complete_items_and_aggregates(monkeypatch):
    source=[item('2026-09-02','new'),item('2026-09-01','old')]
    step=U.UpdateBeliefsStep();calls=[]
    def batch(ctx):
        records=ctx.evidence_bundle.items
        if len(records)>1:raise U.UpdateContextTooLarge('test overflow')
        calls.append(records[0])
        return {'stats':{'updated':1},'contested_keys':['same']}
    monkeypatch.setattr(step,'_run_batch',batch)
    ctx=SimpleNamespace(scope_context=None,evidence_bundle=EvidenceBundle('all','a','b',source))
    result=step.run(ctx)
    assert calls==[source[1],source[0]]
    assert result['stats']['updated']==2 and result['contested_keys']==['same']
    assert ctx.evidence_bundle.items==source

def test_single_oversized_item_stays_pending(monkeypatch):
    step=U.UpdateBeliefsStep()
    monkeypatch.setattr(step,'_run_batch',lambda ctx:(_ for _ in ()).throw(U.UpdateContextTooLarge('full source too large')))
    with pytest.raises(U.UpdateContextTooLarge):
        step.run(SimpleNamespace(scope_context=None,evidence_bundle=EvidenceBundle('all','a','b',[item('today','one')])))

def test_batch_evidence_refs_map_to_own_original_source(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    store=SimpleNamespace(get_by_key=lambda _:None)
    written=[]
    store.upsert_belief=lambda request,evidence:written.append((request,evidence))
    monkeypatch.setattr(U,'BeliefStore',lambda:store)
    monkeypatch.setattr(U,'select_for_evidence',lambda *args:[])
    def respond(msg):
        ref='first' if 'source:first' in msg.agent_input['evidence_block'] else 'second'
        return SimpleNamespace(data={'beliefs':[{'belief_key':'general.'+ref,'domain':'general','action':'create',
            'statement':'complete condition preserved '+ref,'confidence':'medium','status':'active','scope':'chronic',
            'evidence_refs':[1],'evidence_relations':[{'evidence_ref':1,'valence':'support'}]}]})
    monkeypatch.setattr(ServiceLocator,'_services',{'agent_factory':SimpleNamespace(create_agent=lambda _:SimpleNamespace(action_handler=respond))})
    monkeypatch.setattr('belief_engine.config.list_all_domain_ids',lambda:['general'])
    for ref in ('first','second'):
        source=item('2026-09-01','source:'+ref)
        U.UpdateBeliefsStep()._run_batch(SimpleNamespace(scope_context=None,evidence_bundle=EvidenceBundle('all','a','b',[source])))
    assert [ev[0].source_ref for _,ev in written]==['source:first','source:second']

def test_budget_refuses_oversized_input_without_clipping(monkeypatch):
    monkeypatch.setattr(U,'UPDATE_INPUT_BYTES',10)
    payload={'evidence':'original source '*100}
    before=payload.copy()
    with pytest.raises(U.UpdateContextTooLarge):U.check_update_budget(payload)
    assert payload==before

def test_dayflow_pages_keep_whole_context_and_all_selected_records(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    from app.assistant.pipelines.dayflow.steps import dayflow_routine_stage as D
    seen=[]
    def respond(msg):
        seen.append(msg.agent_input)
        return SimpleNamespace(data={'belief_ids':['B0'],'reasoning':'relevant'})
    monkeypatch.setattr(ServiceLocator,'_services',{'agent_factory':SimpleNamespace(create_agent=lambda _:SimpleNamespace(action_handler=respond))})
    entries=[{'belief_key':str(i),'statement':'full text '*4000,'conditions':{'when':'before lesson'}} for i in range(3)]
    selected,_=D._select_beliefs(entries,'full day',None,weekly_insights='full week')
    assert selected==entries and len(seen)==3
    assert all(x['daily_context']=='full day' and x['weekly_insights']=='full week' for x in seen)
    assert seen[2]['belief_catalog'][0]['statement']==entries[2]['statement']


@pytest.fixture(autouse=True)
def isolate_cached_agent_factory(monkeypatch):
    # Other suites may assign the DI proxy directly, shadowing the registry.
    from app.assistant.ServiceLocator.service_locator import DI
    monkeypatch.delitem(DI.__dict__, 'agent_factory', raising=False)
