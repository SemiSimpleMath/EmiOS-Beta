"""Task artifacts reach the next step in the shape the next step declared it wants.

Two defects from the 2026-09-13 morning_briefing failure, both silent:

1. A structured fact substituted whole into an argument the tool declares as TEXT was handed
   over as a dict. `write_text_file` rejected it and the fully-composed briefing was discarded
   on the last step, three runs in a row.
2. An ACTION node's `produces` was dropped — `result_recorder` writes the manager's answer with
   no `data_id` — so `${artifact_1}` (the CNN/BBC scrape) never resolved and the compile step
   was handed the literal text `${artifact_1}` instead of the headlines.

These use the REAL shipped tool contracts, so a change to a declared argument type is caught
here rather than at 05:30 in a nightly run.
"""
import json
from types import SimpleNamespace

import pytest

import app.assistant.tests.test_setup  # noqa: F401 — DI bootstrap, needed for tool_registry
from app.assistant.task_runtime import tool_executor as T
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects.store import WorkStore


@pytest.fixture
def store(tmp_path):
    s = WorkStore(str(tmp_path / 'task_work.db'))
    yield s
    s.close()


# --- declared argument types ------------------------------------------------

def test_write_text_file_declares_content_as_text():
    assert 'content' in T._declared_text_args('write_text_file')
    assert 'file_path' in T._declared_text_args('write_text_file')


def test_save_daily_summary_does_not_declare_its_payload_as_text():
    # The counter-case that stops the coercion becoming a blanket stringify.
    assert 'summary_data' not in T._declared_text_args('save_daily_summary')


def test_a_structured_fact_becomes_text_for_a_declared_text_argument():
    briefing = {'headlines': ['a', 'b'], 'weather': {'high': 81}}
    args = T._substitute_args({'file_path': 'out/x.json', 'content': '${artifact_5}'},
                              {'artifact_5': briefing}, None,
                              text_args=T._declared_text_args('write_text_file'))
    assert isinstance(args['content'], str)
    # Serialised, not stringified via repr — the file must contain valid JSON.
    assert json.loads(args['content']) == briefing


def test_a_structured_fact_stays_structured_for_a_declared_object_argument():
    briefing = {'headlines': ['a']}
    args = T._substitute_args({'summary_data': '${artifact_6}', 'date_str': '2026-09-25'},
                              {'artifact_6': briefing}, None,
                              text_args=T._declared_text_args('save_daily_summary'))
    assert args['summary_data'] == briefing


def test_a_plain_string_fact_is_untouched():
    args = T._substitute_args({'content': '${artifact_2}'}, {'artifact_2': 'already text'}, None,
                              text_args=T._declared_text_args('write_text_file'))
    assert args['content'] == 'already text'


# --- the provenance envelope -------------------------------------------------

def test_invoke_agent_payload_is_unwrapped_from_its_envelope():
    """invoke_agent declares `data.agent_output` as its structured output and puts
    `data.agent_name` beside it. Recording the envelope put the wrapper in the artifact."""
    assert T._declared_payload_key('invoke_agent') == 'agent_output'
    result = ToolResult(result_type='invoke_agent', content='{"x": 1}',
                        data={'agent_name': 'daily_summary::daily_summary',
                              'agent_output': {'x': 1}})
    assert T._payload_of('invoke_agent', result) == {'x': 1}


def test_a_tool_without_a_declared_payload_keeps_its_data():
    result = ToolResult(result_type='success', content='c', data={'a': 1, 'b': 2})
    assert T._payload_of('get_weather', result) == {'a': 1, 'b': 2}


def test_the_recorded_artifact_is_the_payload_not_the_wrapper(store):
    wo = store.apply('create_work_object', {'title': 'compile'})
    store.apply('add_node', {'work_id': wo.id, 'id': 'step_6', 'type': 'tool',
                             'parent_id': wo.goal_node_id, 'title': 'compile',
                             'payload': {'produces': ['artifact_5'],
                                         'tools': [{'tool': 'invoke_agent'}]}})
    node = store.load(wo.id).nodes['step_6']
    T.write_produced_output(store, wo.id, node,
                            ToolResult(result_type='invoke_agent', content='{"briefing": 1}',
                                       data={'agent_name': 'x', 'agent_output': {'briefing': 1}}),
                            tool_name='invoke_agent')
    ev = [n for n in store.load(wo.id).nodes.values()
          if (n.payload or {}).get('data_id') == 'artifact_5']
    assert len(ev) == 1
    assert ev[0].payload['value'] == {'briefing': 1}
    assert 'agent_name' not in ev[0].payload['value']


# --- an action node's produces ----------------------------------------------

def test_an_action_nodes_produces_is_keyed_so_the_next_step_can_read_it(store, monkeypatch):
    from app.assistant.task_runtime.entry import start_task_run
    from app.assistant.task_runtime.task_store import build_task_scope
    import work_objects.discharge as D

    seen = {}

    def fake_discharge(st, work_id, node_id, *, manager_name, scope_context, session_id=None):
        st.apply('set_status', {'work_id': work_id, 'node_id': node_id, 'status': 'done',
                               'reason': 'stub worker finished'}, actor=manager_name)
        return ToolResult(result_type='final_answer', content='scraped',
                          data={'cnn': {'headlines': ['h1']}})

    monkeypatch.setattr(D, 'discharge_node', fake_discharge)

    def fake_tool(st, work_id, node_id, scope, enforced):
        node = st.load(work_id).nodes[node_id]
        seen['args'] = T._substitute_args(
            T._coerce_args(node.payload['tools'][0]),
            T.facts_context(st.load(work_id)), None,
            text_args=T._declared_text_args('write_text_file'))
        st.apply('set_status', {'work_id': work_id, 'node_id': node_id, 'status': 'done',
                               'reason': 'stub'}, actor='task_runner')
        st.apply('set_status', {'work_id': work_id, 'node_id': node_id, 'status': 'closed',
                               'reason': 'stub'}, actor='task_runner')

    monkeypatch.setattr('app.assistant.task_runtime.task_runner.execute_claimed_tool_node',
                        fake_tool)

    template = {
        'task_id': 'scrape_then_save', 'title': 'scrape then save', 'driver': 'task_runner',
        'goal_content': 'Scrape, then save what was scraped.',
        'nodes': [
            {'id': 'scrape', 'type': 'action', 'title': 'Scrape the sites',
             'payload': {'executor': 'playwright_manager', 'produces': ['artifact_1']}},
            {'id': 'save', 'type': 'tool', 'title': 'Save it',
             'payload': {'tools': [{'tool': 'write_text_file',
                                    'args_json': '{"file_path":"out/x.json","content":"${artifact_1}"}'}]}},
        ],
        'edges': [{'src': 'scrape', 'dst': 'save', 'relation': 'depends_on'}],
    }
    start_task_run(template, store=store, scope=build_task_scope('t'),
                   scope_contract_enforced=False)

    # Previously this was the literal "${artifact_1}" — the scrape never became a keyed fact.
    assert seen['args']['content'] != '${artifact_1}'
    assert json.loads(seen['args']['content']) == {'cnn': {'headlines': ['h1']}}
