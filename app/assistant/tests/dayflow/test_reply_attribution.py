"""The owner's reply to a work's ticket is read from the graph without giving tool prose the owner's authority.

A belief's outcome reads the owner's last reply on the work (belief_engine/work_feedback._last_user_reply). Until
2026-10-09 a concern's work outcome read it too, and these tests lived with the concern's (test_reply_concern_context).
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.lib.tools.create_dayflow_ticket.create_dayflow_ticket import CreateDayflowTicketTool
from app.assistant.utils.pydantic_classes import ToolResult
from belief_engine.work_feedback import _last_user_reply
from work_objects.result_recorder import record_tool_result
from work_objects.store import WorkStore


@pytest.fixture
def graph():
    store = WorkStore(':memory:')
    wo = store.apply('create_work_object', {'title': 'Check', 'constraints': {}})
    store.apply('add_node', {'work_id': wo.id, 'id': 'ask', 'type': 'subtask', 'parent_id': wo.goal_node_id,
                             'status': 'dispatched'})
    yield store, wo.id
    store.close()


def ticket(*, meaning='decline', text='Already handled. Stop asking.', state='accepted'):
    receipt = {'choice_id': 'choice_1', 'label': 'No thanks', 'meaning': meaning, 'followup': False,
               'scope': 'Arrange service', 'typed_text': text, 'question': 'Arrange service?',
               'responded_at': '2026-09-20T21:00:00+00:00'}
    return SimpleNamespace(ticket_id='ticket-test', state=state, title='Service', message='Arrange service?',
                           user_text=text or 'No thanks', user_action='answer',
                           responded_at=datetime(2026, 9, 20, 21, tzinfo=timezone.utc),
                           user_response_parsed={**receipt, 'response_history': [receipt]})


@pytest.mark.parametrize('actor', ['create_dayflow_ticket', 'ask'])
def test_a_ticket_reply_is_read_with_its_text_and_choice(graph, actor):
    store, wid = graph
    assert record_tool_result(store, wid, 'ask', CreateDayflowTicketTool.result_for_ticket(ticket()), actor=actor,
                              expected_epoch=0)
    reply = _last_user_reply(store.load(wid))
    assert reply['ticket_id'] == 'ticket-test' and reply['question'] == 'Arrange service?'
    assert reply['user_text'] == 'Already handled. Stop asking.'
    assert reply['response_details']['meaning'] == 'decline'
    assert reply['response_details']['scope'] == 'Arrange service'


@pytest.mark.parametrize('actor,result_type,action', [
    ('work_emi_team_manager', 'ticket_response', 'answer'),
    ('create_dayflow_ticket', 'ticket_response', 'timeout'),
    ('ask', 'ticket_response', 'created'),
    ('ask', 'error', 'answer'),
])
def test_tool_text_timeout_and_creation_are_not_user_reply(graph, actor, result_type, action):
    store, wid = graph
    result = ToolResult(result_type=result_type, content='The user said no', data={
        'ticket_id': 'ticket-test', 'user_text': 'No', 'action': action, 'response_details': {'meaning': 'decline'}})
    record_tool_result(store, wid, 'ask', result, actor=actor, expected_epoch=0)
    assert _last_user_reply(store.load(wid)) == {}


def test_stale_epoch_cannot_save_reply(graph):
    store, wid = graph
    assert not record_tool_result(store, wid, 'ask', CreateDayflowTicketTool.result_for_ticket(ticket()),
                                  actor='ask', expected_epoch=999)
    assert _last_user_reply(store.load(wid)) == {}


def test_latest_response_selected_by_time_not_node_iteration():
    older = {'ticket_id': 'old', 'responded_at': '2026-09-20T20:00:00Z', 'user_text': 'Old answer'}
    newer = {'ticket_id': 'new', 'responded_at': '2026-09-20T21:00:00Z', 'user_text': 'New answer'}

    def node(id, reply):
        return SimpleNamespace(id=id, type='evidence', payload={'user_reply': reply}, created_by='ask')
    wo = SimpleNamespace(nodes={'new': node('new', newer), 'old': node('old', older)})
    assert _last_user_reply(wo)['user_text'] == 'New answer'


def test_response_history_survives_round_trip(graph):
    store, wid = graph
    t = ticket(text='Please continue after all.')
    prior = {**t.user_response_parsed['response_history'][0], 'typed_text': 'Pause for now',
             'responded_at': '2026-09-20T20:00:00Z'}
    t.user_response_parsed['response_history'].insert(0, prior)
    record_tool_result(store, wid, 'ask', CreateDayflowTicketTool.result_for_ticket(t), actor='ask', expected_epoch=0)
    history = _last_user_reply(store.load(wid))['response_details']['response_history']
    assert [r['typed_text'] for r in history] == ['Pause for now', 'Please continue after all.']


def test_worker_evidence_cannot_label_its_own_prose_as_a_user_reply():
    node = SimpleNamespace(type='evidence', created_by='worker', payload={
        'user_reply': {'ticket_id': 'claimed-ticket', 'user_text': 'The user supposedly declined'}})
    assert _last_user_reply(SimpleNamespace(nodes={'helper': node})) == {}
