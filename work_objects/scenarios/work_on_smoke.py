"""
Clean work_on test — drive a WorkObject through the standalone executor arm (work_on)
with the GENERIC work_manager (the emi_team-style delegating worker), NO parallel
orchestrator. This IS the dayflow execution path: dayflow schedules a ready node,
work_on discharges it through the work_manager; results land on the graph.

A 2-step goal (two independent top-level task nodes, both pure-knowledge so the run
needs no web/tools). work_on(store, work_id) drives the ready set sequentially. We
verify the EXECUTION ARM:
  - both top-level nodes reach a terminal status (the generic close path works),
  - the WorkObject rolls up to done (goal = all_owned_children_done),
  - work_on returns "done".

Run from repo root:  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/work_on_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Standing requirement: WorkObject scenarios capture full prompts + LLM results so we never
# debug blind. They tee to the file sink below (the per-agent prompt switch defaults OFF).
os.environ["EMI_PRINT_PROMPTS"] = "1"
os.environ["EMI_PRINT_LLM_RESULTS"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401 - bootstrap DI

from app.assistant.utils.logging_config import add_file_sink
from work_objects.store import WorkStore
from work_objects.work_runtime import work_on


def main() -> None:
    log_path = add_file_sink("work_on_smoke")
    print(f"full logs -> {log_path}\n", flush=True)

    db = os.path.join(tempfile.mkdtemp(prefix="work_objects_"), "w.db")
    store = WorkStore(db)

    wo = store.apply("create_work_object", {
        "title": "two quick facts for a briefing",
        "goal_content": "Gather two quick facts for a briefing.",
        "satisfied_when_kind": "all_owned_children_done",
    }, actor="test")
    goal_id = wo.goal_node_id
    store.apply("set_status", {"work_id": wo.id, "node_id": goal_id, "status": "dispatched"})

    # Two top-level task nodes (parent == goal). Pure-knowledge tasks: a sane planner
    # answers from its own knowledge and returns control — no web/tool delegation needed.
    # satisfied_when omitted -> the node is satisfied by its OWN terminal status (not children).
    store.apply("add_node", {"work_id": wo.id, "type": "subtask", "title": "divide",
                             "content": "What is 144 divided by 12? Give just the number.",
                             "parent_id": goal_id}, actor="planner")
    store.apply("add_node", {"work_id": wo.id, "type": "subtask", "title": "primes",
                             "content": "List the first five prime numbers.",
                             "parent_id": goal_id}, actor="planner")

    print("=== work_on: drive the WorkObject via the generic work_manager (no orchestrator) ===\n", flush=True)
    final_status = work_on(store, wo.id)   # node_id=None -> drive the ready top-level nodes

    final = store.load(wo.id)
    tops = [n for n in final.nodes.values() if n.parent_id == goal_id and n.type == "subtask"]

    print(f"\nwork_on returned: {final_status}", flush=True)
    print(f"WorkObject status: {final.status}", flush=True)
    print("top-level nodes:", flush=True)
    for n in tops:
        print(f"  - [{n.status}] {n.title}", flush=True)

    print("\n=== checks ===", flush=True)

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}", flush=True)
        assert ok, label

    TERMINAL = {"done", "verified", "passed"}
    ck("both top-level nodes reached a terminal status", bool(tops) and all(n.status in TERMINAL for n in tops))
    ck("WorkObject rolled up to done", final.status == "done")
    ck("work_on returned done", final_status == "done")

    store.close()
    print("\nwork_on drove the WorkObject to completion via the generic work_manager — no orchestrator.", flush=True)


if __name__ == "__main__":
    main()
