"""A belief that caused work learns how that work ended.

2026-09-25: a belief said "do not treat the request as completed until he confirms it is
scheduled" while the work object it spawned closed on a single delivery. Neither side could see
the other. These tests pin the link in both directions — provenance in, outcome back — and the
rule that a delivery is not a resolution.
"""
import json
from types import SimpleNamespace

import pytest

from work_objects.store import WorkStore

_KEY = 'routine.reminders.annual_physical.weekly_prompt'


@pytest.fixture
def store(tmp_path):
    s = WorkStore(str(tmp_path / 'work.db'))
    yield s
    s.close()


def graph(store, refs=None):
    wo = store.apply('create_work_object', {
        'title': 'Get the annual physical scheduled',
        'constraints': {'belief_refs': ['x'] if refs is None else refs,
                        'objective': 'Get the owner to schedule his annual physical',
                        'success_criteria': 'the owner confirms the physical is scheduled'}})
    store.apply('add_node', {'work_id': wo.id, 'id': 'main', 'type': 'subtask',
                             'title': 'Remind the owner', 'parent_id': wo.goal_node_id,
                             'status': 'dispatched'})
    store.apply('record_result', {
        'work_id': wo.id, 'node_id': 'main', 'expected_dispatch_epoch': 0,
        'evidence_id': 'reply', 'title': 'User reply', 'answer': 'reminder delivered',
        'status': 'done',
        'user_reply': {'ticket_id': 't', 'question': 'Schedule the physical?',
                       'user_text': 'Booked it for the 14th.',
                       'response_details': {'meaning': 'confirm'}}}, actor='ask')
    return wo.id


def finish(store, wid):
    return store.apply('finalize_task', {
        'work_id': wid, 'node_id': 'main', 'expected_dispatch_epoch': 0,
        'finalizer': {'verdict': 'achieved', 'outcome': 'Reminder delivered.', 'next_step': ''}},
        actor='finalizer')


# --- the outbox --------------------------------------------------------------

def test_terminal_transition_queues_a_receipt_with_the_criterion(store):
    wid = graph(store, [_KEY])
    assert store.pending_belief_feedback() == []
    assert finish(store, wid).status == 'done'

    pending = store.pending_belief_feedback()
    assert len(pending) == 1
    payload = pending[0]['payload']
    assert payload['belief_refs'] == [_KEY]
    assert pending[0]['outcome'] == 'done'
    # The criterion the work closed against travels with the outcome, so the reader can see
    # that a delivery satisfied the work while the belief's own condition may be stricter.
    assert payload['context']['success_criteria'] == 'the owner confirms the physical is scheduled'
    assert payload['context']['objective']
    assert payload['context']['tasks'][0]['finalizer']['verdict'] == 'achieved'
    assert 'Booked it for the 14th.' in json.dumps(payload['reply_nodes'])


def test_no_receipt_when_no_belief_caused_the_work(store):
    wid = graph(store, [])
    assert finish(store, wid).status == 'done'
    assert store.pending_belief_feedback() == []


def test_acknowledge_clears_the_receipt(store):
    wid = graph(store, [_KEY])
    finish(store, wid)
    rid = store.pending_belief_feedback()[0]['id']
    store.acknowledge_belief_feedback(rid)
    assert store.pending_belief_feedback() == []


def test_failed_graph_transaction_leaves_no_receipt(store, monkeypatch):
    wid = graph(store, [_KEY])
    real = store._queue_belief_feedback

    def fail(*args):
        real(*args)
        raise ValueError('transaction failed')

    monkeypatch.setattr(store, '_queue_belief_feedback', fail)
    with pytest.raises(ValueError):
        finish(store, wid)
    assert store.load(wid).status == 'active'
    assert store.pending_belief_feedback() == []


# --- provenance in -----------------------------------------------------------

def test_based_on_belief_prefix_is_lifted_onto_the_work_object(store):
    from app.assistant.dayflow_orchestrator.work_persist import persist_steward_output
    out = persist_steward_output(store, {'new_or_changed': [{
        'work_id': '', 'objective': 'Get the physical scheduled',
        'rationale': 'standing belief', 'success_criteria': 'the owner confirms it is scheduled',
        'based_on': [f'belief:{_KEY}', 'concern:c1', 'artifact_7']}]})
    wo = store.load(out['created'][0]['work_id'])
    # The key is stored bare — it is what BeliefStore.get_by_key takes.
    assert wo.constraints['belief_refs'] == [_KEY]
    # And the concern lane is untouched, prefix and all.
    assert wo.constraints['concern_refs'] == ['concern:c1']


# --- what the model is allowed to say ----------------------------------------

def _agent_returning(payload, monkeypatch):
    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    agent = SimpleNamespace(action_handler=lambda msg: SimpleNamespace(data=payload))
    monkeypatch.setattr(ServiceLocator, '_services',
                        {'agent_factory': SimpleNamespace(create_agent=lambda _: agent)})


def _one_belief():
    return [SimpleNamespace(belief_key=_KEY, statement='s', status='active', confidence='high',
                            scope='temporary', kind='routine_pattern', conditions=None,
                            last_confirmed='2026-09-24')]


@pytest.mark.parametrize('payload,message', [
    ({'outcomes': [{'belief_key': 'something.else', 'action': 'no_change',
                    'valence': 'support', 'reasoning': 'r'}]}, 'unknown belief_key'),
    ({'outcomes': []}, 'omitted belief'),
    ({'outcomes': [{'belief_key': _KEY, 'action': 'revise', 'valence': 'support',
                    'reasoning': 'r', 'statement': '  '}]}, 'without a statement'),
    ({'outcomes': [{'belief_key': _KEY, 'action': 'delete', 'valence': 'support',
                    'reasoning': 'r'}]}, 'invalid action'),
    ({'outcomes': [{'belief_key': _KEY, 'action': 'no_change', 'valence': 'maybe',
                    'reasoning': 'r'}]}, 'invalid valence'),
])
def test_decide_refuses_malformed_decisions(payload, message, monkeypatch):
    from belief_engine import work_feedback as W
    _agent_returning(payload, monkeypatch)
    monkeypatch.setattr(W, '_scope', lambda: None)
    with pytest.raises(ValueError, match=message):
        W._decide(_one_belief(), {'completed_at': '2026-09-25T00:00:00+00:00'}, {})


# --- applying the decision ---------------------------------------------------

_BID = 'B1'


@pytest.fixture()
def catalog(monkeypatch, tmp_path):
    """A scratch intake store holding the one belief that caused the work."""
    from belief_engine import work_feedback as W
    from belief_engine.intake.store import IntakeStore, sqlite_file
    s = IntakeStore(sqlite_file(tmp_path / 'catalog.db'))
    s.apply('2026-09-20', {
        'statement': 'The owner wants weekly prompting until he schedules his annual physical; the '
                     'request is complete only when he confirms it is scheduled.',
        'kind': 'episodic_context', 'scope': 'temporary',
        'sources': [{'time': '2026-09-20 09:00', 'kind': 'said', 'relation': 'support',
                     'text': 'keep reminding me until I book it', 'source_ref': 'message:1'}]},
        [1.0, 0.0], {'verdict': 'new'})
    monkeypatch.setattr(W, '_intake', lambda: s)
    monkeypatch.setattr(W, '_embedder', lambda: (lambda texts: [[0.5, 0.5] for _ in texts]))
    return s


def _deliver(store, wid, monkeypatch, action='resolve', statement='', key=_BID):
    from belief_engine import work_feedback as W
    _agent_returning({'outcomes': [{'belief_key': key, 'action': action, 'valence': 'support',
                                    'reasoning': 'He replied that he booked it for the 14th.',
                                    'statement': statement}]}, monkeypatch)
    monkeypatch.setattr(W, '_scope', lambda: None)
    return W.recover_pending_belief_feedback(store, work_id=wid)


def _work_rows(catalog, wid):
    return [s for b in catalog.beliefs(include_retired=True) for s in b['sources'] if s['kind'] == 'did']


def test_resolve_records_the_work_then_retires_the_belief(store, catalog, monkeypatch):
    wid = graph(store, [_BID])
    finish(store, wid)
    assert _deliver(store, wid, monkeypatch, 'resolve') == 1

    assert store.pending_belief_feedback() == []
    assert catalog.get(_BID)['status'] == 'retired' and catalog.beliefs() == []
    # The reverse link: the belief carries the work id that closed it.
    assert catalog.has_evidence(_BID, f'work:{wid}')
    assert [r['relation'] for r in _work_rows(catalog, wid)] == ['support']


def test_revise_restates_the_belief_and_keeps_the_old_wording(store, catalog, monkeypatch):
    wid = graph(store, [_BID])
    finish(store, wid)
    _deliver(store, wid, monkeypatch, 'revise', statement='The annual physical is booked for the 14th.')
    held = catalog.get(_BID)
    assert held['status'] == 'active' and held['statement'] == 'The annual physical is booked for the 14th.'


def test_no_change_keeps_the_belief_driving_work(store, catalog, monkeypatch):
    wid = graph(store, [_BID])
    finish(store, wid)
    assert _deliver(store, wid, monkeypatch, 'no_change') == 1

    # A delivered reminder must not retire a belief whose condition is the user's confirmation.
    assert catalog.get(_BID)['status'] == 'active'
    assert len(_work_rows(catalog, wid)) == 1


def test_redelivery_does_not_write_twice(store, catalog, monkeypatch):
    wid = graph(store, [_BID])
    finish(store, wid)
    _deliver(store, wid, monkeypatch, 'no_change')

    # Re-queue the same outcome, as a crash between commit and ack would.
    store.apply('set_work_status', {'work_id': wid, 'status': 'abandoned',
                                    'reason': 'test re-close'}, actor='steward')
    _deliver(store, wid, monkeypatch, 'no_change')
    assert len(_work_rows(catalog, wid)) == 1


def test_a_pre_cutover_key_does_not_wedge_the_outbox(store, catalog, monkeypatch):
    wid = graph(store, ['routine.nothing.here'])
    finish(store, wid)
    assert _deliver(store, wid, monkeypatch, 'no_change', key='routine.nothing.here') == 1
    assert store.pending_belief_feedback() == []
    assert _work_rows(catalog, wid) == []


def test_agent_prompts_render_and_carry_the_doctrine():
    """The _decide tests stub the agent, so render the real templates here — a broken
    template would otherwise only surface in a nightly run."""
    from app.assistant.dayflow_orchestrator import work_context

    system = work_context._ENV.get_template(
        'belief_engine/work_outcome/prompts/system.j2').render()
    # The lesson this agent exists for.
    assert 'A delivery is not an outcome' in system
    assert 'Resolve only on evidence the condition was met' in system
    assert 'abandoned work object is not evidence the belief is wrong' in system

    user = work_context._ENV.get_template(
        'belief_engine/work_outcome/prompts/user.j2').render(
        beliefs=[{'belief_key': _KEY, 'statement': 'until he confirms it is scheduled'}],
        work_outcome={'objective': 'o', 'success_criteria': 'c',
                      'user_reply': {'user_text': 'Booked it for the 14th.'}})
    assert _KEY in user
    assert 'until he confirms it is scheduled' in user
    assert 'Booked it for the 14th.' in user


def test_planner_documents_the_outcome_criterion_and_belief_prefix():
    """The doctrine fix: a criterion names the outcome, and belief provenance has a prefix."""
    from app.assistant.agents.dayflow_orchestrator.strategic_planner_wo import agent_form as F
    fields = F.WorkObjectSpec.model_fields
    criterion = fields['success_criteria'].description
    assert 'OUTCOME, not the act' in criterion
    assert 'until he books it' in criterion
    assert 'belief:<belief_key>' in fields['based_on'].description
