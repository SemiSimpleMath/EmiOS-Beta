"""
Standalone live-model smoke scenario: claim -> worker -> recorded result -> finalizer.

Uses a temporary WorkStore, caller-provided scope, the standard worker manager and
the existing work_finalizer agent/Jinja prompts. This tests execution and judgment,
not the complete Dayflow scheduler/architect pipeline. No live Dayflow store is used.
Two knowledge tasks must be finalizer-closed, have recorded evidence and matching
dispatch/finalized epochs, and roll the goal up to done.

Run from repo root with .venv/Scripts/python.exe work_objects/scenarios/work_on_smoke.py.
This explicitly invokes real models and captures prompts/results in the scenario log.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Standing requirement: WorkObject scenarios capture full prompts + LLM results so we never
# debug blind. They tee to the file sink below (the per-agent prompt switch defaults OFF).


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401 - bootstrap DI

from app.assistant.utils.logging_config import add_file_sink
from work_objects.store import WorkStore
from work_objects.discharge import drive_work
from work_objects.scenarios._scenario_scope import scenario_scope


def main() -> None:
    os.environ["EMI_PRINT_PROMPTS"] = "1"
    os.environ["EMI_PRINT_LLM_RESULTS"] = "1"
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
    # Only the finalizer's closed status satisfies each main task.
    store.apply("add_node", {"work_id": wo.id, "type": "subtask", "title": "divide",
                             "content": "What is 144 divided by 12? Give just the number.",
                             "parent_id": goal_id}, actor="planner")
    store.apply("add_node", {"work_id": wo.id, "type": "subtask", "title": "primes",
                             "content": "List the first five prime numbers.",
                             "parent_id": goal_id}, actor="planner")

    print("=== standalone: claim, worker result, finalizer judgment ===\n", flush=True)
    final_status = drive_work(store, wo.id, scope_context=scenario_scope(work_id=wo.id))   # node_id=None -> drive the ready top-level nodes

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

    TERMINAL = {"closed"}
    ck("both top-level nodes reached a terminal status", len(tops) == 2 and all(n.status in TERMINAL for n in tops))
    ck("both results were judged for their dispatch epoch", all(
        n.payload.get("finalized_epoch") == n.payload.get("dispatch_epoch")
        and n.payload.get("finalizer", {}).get("verdict") == "achieved" for n in tops))
    ck("both tasks have recorded result evidence", all(
        any(e.type == "evidence" and e.parent_id == n.id for e in final.nodes.values()) for n in tops))
    ck("WorkObject rolled up to done", final.status == "done")
    ck("work_on returned done", final_status == "done")

    store.close()
    print("\nStandalone worker execution and finalizer judgment completed the goal.", flush=True)


if __name__ == "__main__":
    main()
