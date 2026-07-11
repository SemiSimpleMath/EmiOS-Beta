"""Phase 4 (start) — web-free validation of the re-arm LOOP primitive.

A node with `payload.done_when` re-runs until that condition holds over the recorded facts. Here a
counter node runs, records `count`, and re-arms until `count == '3'`, then closes and the task
completes. Proves loops need no cyclic edges and no node-minting — a node simply doesn't terminalize
until its done-condition is met (each run is an event).

Run:  .venv\\Scripts\\python.exe work_objects\\scenarios\\task_loop_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.assistant.tests.test_setup  # noqa: F401,E402

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects.store import WorkStore
from app.assistant.task_runtime.entry import start_task_run
from app.assistant.task_runtime.task_store import build_task_scope


class _Counter:
    n = 0

    def execute(self, msg):
        _Counter.n += 1
        return ToolResult(content=str(_Counter.n), result_type="success", data={})


def _install():
    oc, of = DI.tool_registry.get_tool_class, DI.tool_registry.get_tool
    DI.tool_registry.get_tool_class = lambda n: _Counter if str(n) == "fake_count" else oc(n)
    DI.tool_registry.get_tool = lambda n: ({"tool_contract": {"metadata": {"min_authority": 50}}}
                                           if str(n) == "fake_count" else of(n))


def test_rearm_loop():
    _Counter.n = 0
    store = WorkStore(path=os.path.join(tempfile.mkdtemp(prefix="taskloop_"), "t.db"))
    template = {
        "task_id": "loop", "title": "loop", "goal_content": "count to 3", "driver": "task_runner",
        "nodes": [
            {"id": "loop", "type": "tool", "title": "loop",
             "payload": {"tools": [{"tool": "fake_count", "args": {}}], "produces": ["count"],
                         "done_when": "count == '3'"}},
            {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}},
        ],
        "edges": [{"src": "loop", "dst": "end", "relation": "depends_on"}],
        "preloaded_facts": [],
    }
    r = start_task_run(template, store=store, scope=build_task_scope("loop"))
    print(f"  [loop]  {r}  runs={_Counter.n}")
    assert r["status"] == "done", r
    assert _Counter.n == 3, f"loop node should have run exactly 3 times, ran {_Counter.n}"
    wo = store.load(r["work_id"])
    assert wo.nodes["loop"].status == "closed"
    assert wo.nodes["end"].status == "closed"
    print("  test_rearm_loop: PASS")


if __name__ == "__main__":
    _install()
    test_rearm_loop()
    print("PHASE 4 RE-ARM LOOP: PASS")
