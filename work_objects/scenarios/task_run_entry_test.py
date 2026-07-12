"""Phase 3 web-free validation of the task run/resume entry (task_runtime.entry.start_task_run).

Proves start_task_run instantiates a work-object TEMPLATE into a task-owned store and drives it to
completion, and that a PARKED run resumes to done — with fake tools, an explicit isolated store, and
the task-execution scope. No dayflow. The live base-timing-engine wake isn't exercised here (that
needs the running app); the park is resumed by advancing the wake and calling resume_task_run,
exactly as the fired wake would.

Run:  .venv\\Scripts\\python.exe work_objects\\scenarios\\task_run_entry_test.py
"""
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.assistant.tests.test_setup  # noqa: F401,E402  bootstrap DI before project imports

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects.store import WorkStore
from work_objects.model import utcnow
from app.assistant.task_runtime.entry import start_task_run, resume_task_run
from app.assistant.task_runtime.task_store import build_task_scope


class _FakeTool:
    def execute(self, msg):
        args = (getattr(msg, "tool_data", {}) or {}).get("arguments", {}) or {}
        return ToolResult(content=str(args.get("emit", "ok")), result_type="success", data={})


def _install_fake_tools():
    orig_cls, orig_cfg = DI.tool_registry.get_tool_class, DI.tool_registry.get_tool
    DI.tool_registry.get_tool_class = lambda n: _FakeTool if str(n).startswith("fake_") else orig_cls(n)
    DI.tool_registry.get_tool = lambda n: ({"tool_contract": {"metadata": {"min_authority": 50}}}
                                           if str(n).startswith("fake_") else orig_cfg(n))


def _template(nodes, edges):
    return {"task_id": "t", "title": "t", "goal_content": "g", "driver": "task_runner",
            "nodes": nodes, "edges": edges, "preloaded_facts": []}


def _store():
    return WorkStore(path=os.path.join(tempfile.mkdtemp(prefix="taskrun3_"), "t.db"))


def test_start_completes():
    tmpl = _template(
        [{"id": "a", "type": "tool", "title": "a",
          "payload": {"tools": [{"tool": "fake_gather", "args": {"emit": "x"}}], "produces": ["d1"]}},
         {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}}],
        [{"src": "a", "dst": "end", "relation": "depends_on"}])
    store = _store()
    r = start_task_run(tmpl, store=store, scope=build_task_scope("t"))
    print(f"  [start]  {r}")
    assert r["status"] == "done", r
    assert store.load(r["work_id"]).status == "done"
    assert store.load(r["work_id"]).constraints.get("driver") == "task_runner"
    print("  test_start_completes: PASS")


def test_park_and_resume():
    store, scope = _store(), build_task_scope("t")
    tmpl = _template(
        [{"id": "a", "type": "tool", "title": "a", "payload": {"tools": [{"tool": "fake_gather", "args": {"emit": "x"}}]}},
         {"id": "wait", "type": "tool", "title": "wait", "payload": {},
          "wake_kind": "time", "wake_at": utcnow() + timedelta(hours=1)},
         {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}}],
        [{"src": "a", "dst": "end", "relation": "depends_on"},
         {"src": "wait", "dst": "end", "relation": "depends_on"}])
    r = start_task_run(tmpl, store=store, scope=scope)
    print(f"  [park]   {r}")
    assert r["status"] == "parked", r
    wid = r["work_id"]
    # ids are namespaced per instance (tid--<wid6>)
    wait_id = next(n.id for n in store.load(wid).nodes.values() if n.id.split("--")[0] == "wait")
    # simulate the base-timing-engine wake firing: advance the wait to due, then resume
    store.apply("defer_node", {"work_id": wid, "node_id": wait_id, "wake_kind": "time",
                               "wake_at": utcnow() - timedelta(seconds=1)}, actor="test")
    r2 = resume_task_run(wid, store=store, scope=scope)
    print(f"  [resume] {r2}")
    assert r2["status"] == "done", r2
    print("  test_park_and_resume: PASS")


if __name__ == "__main__":
    _install_fake_tools()
    test_start_completes()
    test_park_and_resume()
    print("PHASE 3 TASK-RUN ENTRY: ALL PASS")
