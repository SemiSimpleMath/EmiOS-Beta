"""Phase 4 — web-free validation of the wait_gate + (forward) decision mapping.

A synthetic compiled task: act -> wait_gate(E or C) -> decision(C? handle_C : handle_E) -> end.
Proves build_template maps a wait_gate to an event-gated wait node and a decision to mutually-exclusive
guards on its branch targets; and that the runner PARKS on the event, then on resume (event 'E' recorded)
releases the wait, takes the not-C branch, abandons the C branch, and completes. Also checks that a
decision branching BACK to an earlier step (a loop) is refused (loop-collapse is the remaining piece).

Run:  .venv\\Scripts\\python.exe work_objects\\scenarios\\task_wait_decision_test.py
"""
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


def test_loop_decision_raises():
    looped = {"task_id": "loop", "source_task": "loop", "entry_step_id": "s1",
              "steps": [
                  {"id": "s1", "kind": "tool_sequence", "title": "s1",
                   "tools": [{"tool": "fake_g", "args": {}}], "next_step": "s_dec"},
                  {"id": "s_dec", "kind": "decision", "title": "d", "condition": '"C" in task_state.facts.events_observed',
                   "on_true": "s_end", "on_false": "s1"},   # on_false -> s1 (earlier) = LOOP
                  {"id": "s_end", "kind": "end", "title": "end"},
              ], "data_bindings": [], "preloaded_task_state": {"facts": {}, "artifacts": {}, "flags": {}}}
    try:
        build_template(looped)
    except NotImplementedError:
        print("  test_loop_decision_raises: PASS")
        return
    raise AssertionError("expected NotImplementedError for a decision back-edge (loop)")


if __name__ == "__main__":
    _install()
    test_wait_and_forward_decision()
    test_loop_decision_raises()
    print("PHASE 4 WAIT+DECISION: ALL PASS")
