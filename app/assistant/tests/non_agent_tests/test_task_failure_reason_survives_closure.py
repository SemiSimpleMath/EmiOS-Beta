"""A dead task run reports WHY, not a phantom stall.

2026-09-13: three morning_briefing runs died at the final save step with
`tool 'write_text_file' errored: Missing required argument: content`, recorded correctly on the
node. `entry._after_drive` then abandoned the work object, the closure cascade rewrote that node
from `failed` to `abandoned`, and only afterwards did the caller ask `failure_reason()` — which
looks for nodes still at `failed`, found none, and returned its generic
"no failed node recorded (stalled graph)" string. That generic message is what auto-disabled the
routine and sent the auditor chasing a dispatch stall that never happened.
"""
import pytest

from app.assistant.task_runtime.entry import start_task_run
from app.assistant.task_runtime.task_store import build_task_scope
from work_objects.store import WorkStore


@pytest.fixture
def store(tmp_path):
    s = WorkStore(str(tmp_path / 'task_work.db'))
    yield s
    s.close()


def _template():
    """One tool node naming a tool that is not registered — the executor fails the node with a
    specific message, exactly the shape a real tool-argument rejection produces."""
    return {
        'task_id': 'failing_task', 'title': 'failing task', 'driver': 'task_runner',
        'goal_content': 'Save something that cannot be saved.',
        'nodes': [{'id': 'step_save', 'type': 'tool', 'title': 'Save the briefing',
                   'payload': {'tools': [{'tool': 'no_such_tool_exists',
                                          'args_json': '{"content": "x"}'}]}}],
        'edges': [],
    }


def _run(store):
    return start_task_run(_template(), store=store, scope=build_task_scope('t'),
                          scope_contract_enforced=False)


def test_the_real_error_reaches_the_caller(store):
    result = _run(store)

    assert result['status'] == 'failed'
    reason = result['failure_reason']
    # The specific cause, not the generic string. Assert on the failing node id and on the
    # recorded error being present at all — the exact tool-layer wording differs between a
    # registry miss and an argument rejection, and the contract is that WHICHEVER it was
    # survives the closure.
    assert 'no failed node recorded' not in reason
    assert 'step_save' in reason
    assert reason.split(':', 1)[1].strip()


def test_the_terminal_reason_keeps_the_cause_in_the_graph(store):
    result = _run(store)
    wo = store.load(result['work_id'])

    assert wo.status == 'abandoned'
    goal = wo.nodes[wo.goal_node_id]
    terminal = (goal.payload or {}).get('terminal') or {}
    # The old text was a bare "run cancelled/failed at entry", which said nothing and was also
    # untrue — nothing had been cancelled.
    assert 'step_save' in terminal['reason']
    assert 'run failed' in terminal['reason']
    assert 'cancelled' not in terminal['reason']


def test_reading_the_graph_afterwards_is_still_lossy(store):
    """Why the fix had to move the read earlier rather than reword the message: once the work
    object is abandoned the failed node is gone, and the graph genuinely cannot answer."""
    from app.assistant.task_runtime.task_runner import failure_reason
    result = _run(store)

    after = failure_reason(store, result['work_id'])
    assert after == 'no failed node recorded (stalled graph)'
    # ...which is precisely what the callers used to report instead of the real cause.
    assert result['failure_reason'] != after


def test_a_healthy_run_reports_no_reason(store):
    template = _template()
    template['nodes'] = [{'id': 'step_end', 'type': 'tool', 'title': 'End',
                          'payload': {'is_end': True}}]
    result = start_task_run(template, store=store, scope=build_task_scope('t'),
                            scope_contract_enforced=False)
    assert result['status'] == 'done'
    assert result['failure_reason'] == ''
