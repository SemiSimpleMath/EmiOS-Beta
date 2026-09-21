"""Actual ticket replies survive result commits without giving tool prose user authority."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from work_objects.store import WorkStore
from work_objects.result_recorder import record_tool_result
from app.assistant.utils.pydantic_classes import ToolResult
from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
from app.assistant.subconscious.concern_feedback import _last_user_reply, propagate_work_outcome
from app.assistant.subconscious.persist import apply_work_outcome
from app.assistant.tests.dayflow.test_concern_propagation import _register, _CID


@pytest.fixture
def graph():
    store=WorkStore(':memory:')
    wo=store.apply('create_work_object', {'title':'Check concern', 'constraints':{'concern_refs':[_CID]}})
    store.apply('add_node',{'work_id':wo.id,'id':'ask','type':'subtask',
        'parent_id':wo.goal_node_id,'status':'dispatched'})
    yield store,wo.id
    store.close()


def ticket(*, meaning='decline', text='Already handled. Stop asking.', state='accepted'):
    receipt={'choice_id':'choice_1','label':'No thanks','meaning':meaning,
        'followup':False,'scope':'Arrange service','typed_text':text,'question':'Arrange service?',
        'responded_at':'2026-09-20T21:00:00+00:00'}
    return SimpleNamespace(ticket_id='ticket-test',state=state,title='Service',message='Arrange service?',
        user_text=text or 'No thanks',user_action='answer',responded_at=datetime(2026,9,20,21,tzinfo=timezone.utc),
        user_response_parsed={**receipt,'response_history':[receipt]})


@pytest.mark.parametrize('actor',['create_dayflow_ticket','ask'])
@pytest.mark.parametrize('outcome',['abandoned','done'])
def test_ticket_to_graph_to_concern_preserves_text_and_choice(graph,tmp_path,monkeypatch,actor,outcome):
    store,wid=graph
    result=CreateDayflowTicketTool.result_for_ticket(ticket())
    assert record_tool_result(store,wid,'ask',result,actor=actor,expected_epoch=0)
    reply=_last_user_reply(store.load(wid))
    assert reply['ticket_id']=='ticket-test'
    assert reply['question']=='Arrange service?'
    assert reply['user_text']=='Already handled. Stop asking.'
    assert reply['response_details']['meaning']=='decline'
    assert reply['response_details']['scope']=='Arrange service'
    path=_register(tmp_path)
    from app.assistant.subconscious import persist, answer_capture
    real=persist.apply_work_outcome
    monkeypatch.setattr(persist,'apply_work_outcome',lambda *a,**kw:real(*a,**kw,register_path=path))
    trigger=Mock()
    monkeypatch.setattr(answer_capture,'trigger_noticer',trigger)
    store.apply('set_work_status', {'work_id':wid,'status':outcome,'reason':'User discussion settled'})
    propagate_work_outcome(store,wid,outcome)
    reg=json.loads(path.read_text())
    concern=next(c for bucket in ('active','addressing','dormant','resolved') for c in reg[bucket])
    assert 'Already handled. Stop asking.' in concern['reinforcement_notes']
    assert 'Arrange service' in concern['reinforcement_notes']
    assert 'decline' in concern['reinforcement_notes']
    assert not concern.get('user_declined_at_utc')  # Written qualifiers require interpretation.
    trigger.assert_called_once()


@pytest.mark.parametrize('actor,result_type,action',[
    ('work_emi_team_manager','ticket_response','answer'),
    ('create_dayflow_ticket','ticket_response','timeout'),
    ('ask','ticket_response','created'),
    ('ask','error','answer'),
])
def test_tool_text_timeout_and_creation_are_not_user_reply(graph,actor,result_type,action):
    store,wid=graph
    result=ToolResult(result_type=result_type,content='The user said no',data={
        'ticket_id':'ticket-test','user_text':'No','action':action,'response_details':{'meaning':'decline'}})
    record_tool_result(store,wid,'ask',result,actor=actor,expected_epoch=0)
    assert _last_user_reply(store.load(wid))=={}


def test_stale_epoch_cannot_save_reply(graph):
    store,wid=graph
    assert not record_tool_result(store,wid,'ask',CreateDayflowTicketTool.result_for_ticket(ticket()),
        actor='ask',expected_epoch=999)
    assert _last_user_reply(store.load(wid))=={}


def test_latest_response_selected_by_time_not_node_iteration():
    older={'ticket_id':'old','responded_at':'2026-09-20T20:00:00Z','user_text':'Old answer'}
    newer={'ticket_id':'new','responded_at':'2026-09-20T21:00:00Z','user_text':'New answer'}
    def node(id,reply):
        return SimpleNamespace(id=id,type='evidence',payload={'user_reply':reply},created_by='ask')
    wo=SimpleNamespace(nodes={'new':node('new',newer),'old':node('old',older)})
    assert _last_user_reply(wo)['user_text']=='New answer'


@pytest.mark.parametrize('meaning,text',[
    ('acknowledge',''),('no',''),('approve',''),('handle_myself',''),
    ('decline','Actually please continue.'),('answer','OK'),
])
def test_ambiguous_or_qualified_response_does_not_silence_concern(tmp_path,meaning,text):
    path=_register(tmp_path)
    response={'user_text':text or 'OK','response_details':{'meaning':meaning,'typed_text':text,
        'label':'OK','scope':'Current question'}}
    assert apply_work_outcome(_CID,work_id='work',outcome='abandoned',
        user_response=response,register_path=path)=='journaled'
    reg=json.loads(path.read_text())
    assert len(reg['active'])==1 and reg['dormant']==[]
    assert meaning in reg['active'][0]['reinforcement_notes']


def test_historical_words_are_preserved_without_inventing_decline(tmp_path):
    path=_register(tmp_path)
    assert apply_work_outcome(_CID,work_id='work',outcome='abandoned',
        user_words='OK',register_path=path)=='journaled'
    assert 'OK' in json.loads(path.read_text())['active'][0]['reinforcement_notes']


def test_response_history_survives_round_trip(graph):
    store,wid=graph
    t=ticket(text='Please continue after all.')
    prior={**t.user_response_parsed['response_history'][0], 'typed_text':'Pause for now',
        'responded_at':'2026-09-20T20:00:00Z'}
    t.user_response_parsed['response_history'].insert(0,prior)
    record_tool_result(store,wid,'ask',CreateDayflowTicketTool.result_for_ticket(t),actor='ask',expected_epoch=0)
    history=_last_user_reply(store.load(wid))['response_details']['response_history']
    assert [r['typed_text'] for r in history]==['Pause for now','Please continue after all.']


def test_worker_evidence_cannot_label_its_own_prose_as_a_user_reply():
    node=SimpleNamespace(type='evidence',created_by='worker',payload={
        'user_reply':{'ticket_id':'claimed-ticket','user_text':'The user supposedly declined'}})
    assert _last_user_reply(SimpleNamespace(nodes={'helper':node}))=={}
