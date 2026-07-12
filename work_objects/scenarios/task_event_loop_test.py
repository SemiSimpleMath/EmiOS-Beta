"""Phase 4 — web-free validation of the LOOP-NODE RUNTIME (wake-promotion).

A re-arming loop node (like katy's collapsed handler): run the action, then park on its events; each
delivered event promotes it to run ONCE more (wake-promotion, NOT guard-over-accumulated-events, so it
can't re-fire on a stale event); it closes when done_when holds (the cutoff event arrives).

  start          -> run #1 (handle), park                 (status: parked)
  resume(reply)  -> run #2 (handle), park                 (parked)
  resume(cutoff) -> run #3, done_when('cutoff' observed)  -> close -> end -> done

Run:  .venv\\Scripts\\python.exe work_objects\\scenarios\\task_event_loop_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.assistant.tests.test_setup  # noqa: F401,E402

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects.store import WorkStore
from app.assistant.task_runtime.entry import start_task_run, resume_task_run
from app.assistant.task_runtime.task_store import build_task_scope


class _Handler:
    runs = 0

    def execute(self, msg):
        _Handler.runs += 1
        return ToolResult(content="handled", result_type="success", data={})


def _install():
    oc, of = DI.tool_registry.get_tool_class, DI.tool_registry.get_tool
    DI.tool_registry.get_tool_class = lambda n: _Handler if str(n) == "fake_handle" else oc(n)
    DI.tool_registry.get_tool = lambda n: ({"tool_contract": {"metadata": {"min_authority": 50}}}
                                           if str(n) == "fake_handle" else of(n))


def test_event_loop_runtime():
    _Handler.runs = 0
    store = WorkStore(path=os.path.join(tempfile.mkdtemp(prefix="taskev_"), "t.db"))
    scope = build_task_scope("kloop")
    template = {
        "task_id": "kloop", "title": "kloop", "goal_content": "reply until cutoff", "driver": "task_runner",
        "nodes": [
            {"id": "loop", "type": "tool", "title": "handle loop", "wake_kind": "event",
             "payload": {"tools": [{"tool": "fake_handle", "args": {}}], "is_loop": True,
                         "done_when": '"cutoff" in events_observed', "subscriptions": ["reply", "cutoff"]}},
            {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}},
        ],
        "edges": [{"src": "loop", "dst": "end", "relation": "depends_on"}], "preloaded_facts": [],
    }

    r = start_task_run(template, store=store, scope=scope)
    print(f"  [start]        {r}  runs={_Handler.runs}")
    assert r["status"] == "parked" and _Handler.runs == 1, (r, _Handler.runs)
    wid = r["work_id"]

    r2 = resume_task_run(wid, observed_event="reply", store=store, scope=scope)
    print(f"  [reply]        {r2}  runs={_Handler.runs}")
    assert r2["status"] == "parked" and _Handler.runs == 2, (r2, _Handler.runs)

    # a stale re-drive with NO new event must NOT re-fire the loop (wake-promotion, not accumulated guard)
    r_stale = resume_task_run(wid, store=store, scope=scope)
    assert r_stale["status"] == "parked" and _Handler.runs == 2, ("stale re-drive re-fired the loop", _Handler.runs)

    r3 = resume_task_run(wid, observed_event="cutoff", store=store, scope=scope)
    print(f"  [cutoff]       {r3}  runs={_Handler.runs}")
    assert r3["status"] == "done" and _Handler.runs == 3, (r3, _Handler.runs)
    wo = store.load(wid)
    _iid = lambda tid: next(n.id for n in wo.nodes.values() if n.id.split("--")[0] == tid)
    assert wo.nodes[_iid("loop")].status == "closed" and wo.nodes[_iid("end")].status == "closed"
    print("  test_event_loop_runtime: PASS")


if __name__ == "__main__":
    _install()
    test_event_loop_runtime()
    print("PHASE 4 LOOP RUNTIME: PASS")
