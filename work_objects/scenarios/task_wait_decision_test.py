"""Phase 4 — web-free validation of the wait_gate + (forward) decision mapping.

A synthetic compiled task: act -> wait_gate(E or C) -> decision(C? handle_C : handle_E) -> end.
Proves build_template maps a wait_gate to an event-gated wait node and a decision to mutually-exclusive
guards on its branch targets; and that the runner PARKS on the event, then on resume (event 'E' recorded)
releases the wait, takes the not-C branch, abandons the C branch, and completes. Also checks that a
decision branching BACK to an earlier step (a loop) is refused (loop-collapse is the remaining piece).

Run:  .venv\\Scripts\\python.exe work_objects\\scenarios\\task_wait_decision_test.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.assistant.tests.test_setup  # noqa: F401,E402

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects.store import WorkStore
from app.assistant.task_runtime.wo_builder import build_template
from app.assistant.task_runtime.entry import start_task_run, resume_task_run
from app.assistant.task_runtime.task_store import build_task_scope


class _FakeTool:
    def execute(self, msg):
        args = (getattr(msg, "tool_data", {}) or {}).get("arguments", {}) or {}
        return ToolResult(content=str(args.get("emit", "ok")), result_type="success", data={})


def _install():
    oc, of = DI.tool_registry.get_tool_class, DI.tool_registry.get_tool
    DI.tool_registry.get_tool_class = lambda n: _FakeTool if str(n).startswith("fake_") else oc(n)
    DI.tool_registry.get_tool = lambda n: ({"tool_contract": {"metadata": {"min_authority": 50}}}
                                           if str(n).startswith("fake_") else of(n))


_COMPILED = {
    "task_id": "wd", "source_task": "wait+decision test", "entry_step_id": "s_act",
    "steps": [
        {"id": "s_act", "kind": "tool_sequence", "title": "act",
         "tools": [{"tool": "fake_g", "args": {"emit": "x"}}], "next_step": "s_wait"},
        {"id": "s_wait", "kind": "wait_gate", "title": "wait", "subscriptions": ["E", "C"],
         "release_condition": '("E" in task_state.facts.events_observed) or ("C" in task_state.facts.events_observed)',
         "next_step": "s_dec"},
        {"id": "s_dec", "kind": "decision", "title": "dec",
         "condition": '"C" in task_state.facts.events_observed', "on_true": "s_c", "on_false": "s_e"},
        {"id": "s_c", "kind": "tool_sequence", "title": "handle C",
         "tools": [{"tool": "fake_g", "args": {"emit": "c"}}], "next_step": "s_end"},
        {"id": "s_e", "kind": "tool_sequence", "title": "handle E",
         "tools": [{"tool": "fake_g", "args": {"emit": "e"}}], "next_step": "s_end"},
        {"id": "s_end", "kind": "end", "title": "end"},
    ],
    "data_bindings": [], "preloaded_task_state": {"facts": {}, "artifacts": {}, "flags": {}},
}


def test_wait_and_forward_decision():
    template = build_template(_COMPILED)
    store = WorkStore(path=os.path.join(tempfile.mkdtemp(prefix="taskwd_"), "t.db"))
    scope = build_task_scope("wd")
    r = start_task_run(template, store=store, scope=scope)
    print(f"  [start]  {r}")
    assert r["status"] == "parked", f"should park on the event gate, got {r}"
    wid = r["work_id"]
    assert store.load(wid).nodes["s_wait"].status in ("proposed", "waiting"), "wait node should be parked"

    # event 'E' fires -> release the gate; decision condition is 'C in events' -> False -> the on_false
    # branch (s_e) runs, and the on_true branch (s_c) is abandoned.
    r2 = resume_task_run(wid, observed_event="E", store=store, scope=scope)
    print(f"  [resume] {r2}")
    assert r2["status"] == "done", r2
    wo = store.load(wid)
    assert wo.nodes["s_wait"].status == "closed"
    assert wo.nodes["s_e"].status == "closed", f"E branch should run, got {wo.nodes['s_e'].status}"
    assert wo.nodes["s_c"].status == "abandoned", f"C branch should be abandoned, got {wo.nodes['s_c'].status}"
    assert wo.status == "done"
    print("  test_wait_and_forward_decision: PASS")


def test_katy_loop_collapses():
    """The REAL auto_reply_katy compiled task: action_1 -> wait_gate -> decision(cutoff? end : loop->action_1)
    collapses to a single re-arming loop node keyed by the loop start, with the wait_gate + decision folded in."""
    katy = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "tasks", "auto_reply_katy", "auto_reply_katy.json")
    from app.assistant.task_runtime.wo_builder import build_template as _bt
    template = _bt(json.loads(open(katy, encoding="utf-8").read()))
    by_id = {n["id"]: n for n in template["nodes"]}

    loop = by_id.get("step_action_1")
    assert loop is not None and loop["payload"].get("is_loop"), "action_1 should be the collapsed loop node"
    assert "22_00" in str(loop["payload"].get("done_when")), loop["payload"].get("done_when")
    assert any(t.get("tool") == "invoke_agent" for t in (loop["payload"].get("tools") or [])), "action -> invoke_agent"
    assert loop["payload"].get("release_condition"), "loop carries the wait's release condition"
    # the wait_gate and decision are folded in — no standalone nodes
    assert "step_wait_gate_1" not in by_id and "step_decision_1" not in by_id
    # the end node remains and is reached from the loop node
    assert "step_end_1" in by_id
    end_deps = [e["src"] for e in template["edges"] if e["dst"] == "step_end_1" and e["relation"] == "depends_on"]
    assert "step_action_1" in end_deps, f"end should depend on the loop node; deps={end_deps}"
    # preloaded facts/artifacts carried through
    assert {"fact_1", "fact_2", "artifact_1", "artifact_2"} <= {f["data_id"] for f in template["preloaded_facts"]}
    print(f"  nodes={[n['id'] for n in template['nodes']]}")
    print("  test_katy_loop_collapses: PASS")


def test_state_predicate_loop_collapses():
    # a decision back-edge whose loop body has NO wait is a state-predicate loop:
    # it collapses to a tight re-arm node (is_loop + done_when, no wait / no subscriptions).
    looped = {"task_id": "loop", "source_task": "loop", "entry_step_id": "s1",
              "steps": [
                  {"id": "s1", "kind": "tool_sequence", "title": "s1",
                   "tools": [{"tool": "fake_g", "args": {}}], "next_step": "s_dec"},
                  {"id": "s_dec", "kind": "decision", "title": "d", "condition": '"C" in task_state.facts.events_observed',
                   "on_true": "s_end", "on_false": "s1"},   # on_false -> s1 (earlier) = LOOP, no wait in the body
                  {"id": "s_end", "kind": "end", "title": "end"},
              ], "data_bindings": [], "preloaded_task_state": {"facts": {}, "artifacts": {}, "flags": {}}}
    template = build_template(looped)
    by_id = {n["id"]: n for n in template["nodes"]}
    loop = by_id.get("s1")
    assert loop is not None and loop["payload"].get("is_loop"), "s1 should be the collapsed loop node"
    assert str(loop["payload"].get("done_when") or "").strip(), "state-predicate loop carries a done_when"
    assert loop.get("wake_kind") is None, "a no-wait loop does not park on events"
    assert not loop["payload"].get("subscriptions"), "a no-wait loop has no subscriptions"
    assert "s_dec" not in by_id, "the decision folds into the loop node"
    print("  test_state_predicate_loop_collapses: PASS")


def test_multiwait_loop_raises():
    # a loop body with more than one wait can't collapse to a single re-arming node -> fail loud
    looped = {"task_id": "loop", "source_task": "loop", "entry_step_id": "s1",
              "steps": [
                  {"id": "s1", "kind": "tool_sequence", "title": "s1",
                   "tools": [{"tool": "fake_g", "args": {}}], "next_step": "s_w1"},
                  {"id": "s_w1", "kind": "wait_for_event", "title": "w1",
                   "event_name": "signal_router.watch.a", "next_step": "s_w2"},
                  {"id": "s_w2", "kind": "wait_for_event", "title": "w2",
                   "event_name": "signal_router.watch.b", "next_step": "s_dec"},
                  {"id": "s_dec", "kind": "decision", "title": "d", "condition": '"C" in task_state.facts.events_observed',
                   "on_true": "s_end", "on_false": "s1"},   # on_false -> s1 = LOOP with two waits in the body
                  {"id": "s_end", "kind": "end", "title": "end"},
              ], "data_bindings": [], "preloaded_task_state": {"facts": {}, "artifacts": {}, "flags": {}}}
    try:
        build_template(looped)
    except NotImplementedError:
        print("  test_multiwait_loop_raises: PASS")
        return
    raise AssertionError("expected NotImplementedError for a loop body with more than one wait")


if __name__ == "__main__":
    _install()
    test_wait_and_forward_decision()
    test_katy_loop_collapses()
    test_state_predicate_loop_collapses()
    test_multiwait_loop_raises()
    print("PHASE 4 WAIT+DECISION+LOOP: ALL PASS")
