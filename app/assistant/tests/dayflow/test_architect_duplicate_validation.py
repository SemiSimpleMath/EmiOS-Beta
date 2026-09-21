"""Malformed consolidation must not acknowledge a revision or alter its graph."""
import pytest
from work_objects.store import WorkStore
from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag
from app.assistant.control_nodes.work_architect_node import _duplicate_pairs


@pytest.fixture
def graph():
    store=WorkStore(':memory:')
    wo=store.apply('create_work_object',{'title':'Consolidate work'})
    for nid in ('keep','duplicate','other','before','after'):
        store.apply('add_node',{'work_id':wo.id,'id':nid,'type':'subtask',
            'parent_id':wo.goal_node_id,'status':'actionable'})
    store.apply('add_node',{'work_id':wo.id,'id':'helper','type':'subtask','parent_id':'duplicate'})
    store.apply('set_status',{'work_id':wo.id,'node_id':'other','status':'actionable',
        'finalizer':{'verdict':'retry','next_step':'retry','outcome':'Consolidate duplicated work'}})
    for src,dst in [('before','duplicate'),('duplicate','after')]:
        store.apply('add_edge',{'work_id':wo.id,'src':src,'dst':dst,'relation':'depends_on'})
    yield store,wo.id
    store.close()


def attempt(store,wid,mapping,**kwargs):
    wo=store.load(wid)
    entry={'node_id':'other',**wo.nodes['other'].payload['finalizer']}
    return apply_architect_dag(store,wid,[{'node_id':'new','title':'New work'}],
        duplicate_of=mapping,finalizer_instructions=[entry],expected_updated_at=wo.updated_at,**kwargs)


@pytest.mark.parametrize('mapping',[
    {'duplicate':'missing'}, {'missing':'keep'}, {'':'keep'}, {'duplicate':''},
    {'duplicate':'duplicate'}, {'duplicate':'helper'}, {'helper':'keep'},
    {'duplicate':'keep','other':'missing'}, {'duplicate':'keep','keep':'duplicate'},
    {'duplicate':'keep','keep':'other'}, {'duplicate':'keep',' duplicate ':'other'},
])
def test_invalid_mapping_leaves_graph_events_and_finalizer_untouched(graph,mapping):
    store,wid=graph
    before=store.load(wid).model_dump(mode='json')
    events=store.events(wid)
    with pytest.raises(ValueError):
        attempt(store,wid,mapping)
    assert store.load(wid).model_dump(mode='json')==before
    assert store.events(wid)==events
    assert store.load(wid).has_pending_revision()


@pytest.mark.parametrize('side',['duplicate','keep'])
@pytest.mark.parametrize('status',['done','closed','abandoned','superseded'])
def test_finished_endpoint_rejects_entire_revision(graph,side,status):
    store,wid=graph
    # Isolated fixture setup: build a finished historical node without running a real task.
    with store._conn:
        store._conn.execute('UPDATE nodes SET status=? WHERE id=?',(status,side))
    before=store.load(wid).model_dump(mode='json')
    with pytest.raises(ValueError,match='finished task'):
        attempt(store,wid,{'duplicate':'keep'})
    assert store.load(wid).model_dump(mode='json')==before
    assert store.load(wid).has_pending_revision()


def test_retained_node_cannot_be_explicitly_abandoned(graph):
    store,wid=graph
    before=store.load(wid).model_dump(mode='json')
    with pytest.raises(ValueError,match='also drops'):
        attempt(store,wid,{'duplicate':'keep'},abandon_node_ids=[' keep '],licensed=True)
    assert store.load(wid).model_dump(mode='json')==before


def test_goal_is_not_a_duplicate_endpoint(graph):
    store,wid=graph
    before=store.load(wid).model_dump(mode='json')
    with pytest.raises(ValueError):
        attempt(store,wid,{'duplicate':store.load(wid).goal_node_id})
    assert store.load(wid).model_dump(mode='json')==before


def test_valid_consolidation_preserves_dependencies_and_commits_acknowledgment(graph):
    store,wid=graph
    result=attempt(store,wid,{'duplicate':'keep'})
    wo=store.load(wid)
    assert result['deduplicated']==['duplicate']
    assert wo.nodes['duplicate'].status=='abandoned'
    assert wo.nodes['helper'].status=='abandoned'
    assert wo.nodes['keep'].status=='actionable'
    assert wo.deps_of('keep')==['before']
    assert wo.deps_of('after')==['keep']
    assert not wo.has_pending_revision()
    assert store.events(wid)[-1]['op']=='batch'


@pytest.mark.parametrize('pairs',[
    {'duplicate':'keep'}, [None], ['duplicate'], [{}],
    [{'duplicate_node_id':1,'keep_node_id':'keep'}],
    [{'duplicate_node_id':' ','keep_node_id':'keep'}],
    [{'duplicate_node_id':'duplicate','keep_node_id':' duplicate '}],
    [{'duplicate_node_id':'duplicate','keep_node_id':'keep'},
     {'duplicate_node_id':' duplicate ','keep_node_id':'other'}],
])
def test_pair_conversion_never_silently_drops_or_overwrites_input(pairs):
    with pytest.raises(ValueError):
        _duplicate_pairs({'duplicate_of':pairs})


def test_pair_conversion_accepts_absent_empty_and_valid_pairs():
    assert _duplicate_pairs({})=={}
    assert _duplicate_pairs({'duplicate_of':[]})=={}
    assert _duplicate_pairs({'duplicate_of':[
        {'duplicate_node_id':' duplicate ','keep_node_id':' keep '},
        {'duplicate_node_id':'other','keep_node_id':'keep'}]})=={'duplicate':'keep','other':'keep'}
