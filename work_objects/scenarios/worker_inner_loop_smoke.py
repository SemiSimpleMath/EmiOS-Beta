"""
WEB-FREE inner-loop smoke (no search quota): a single first-class WorkerPlanner owns
one node, lists its checklist (synced to subtask nodes by the WorkPlanner reconcile
hook, rendered back by workobject_render_node), works the items in sequence via the
single action channel, marks them done, and finishes.

    PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/worker_inner_loop_smoke.py
Pair with EMI_PRINT_LLM_RESULTS=1 to see each WorkerPlanner decision.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401 - bootstrap DI

from work_objects.store import WorkStore
from work_objects.discharge import discharge_node
from work_objects.scenarios._scenario_scope import scenario_scope

REQUEST = ("Write three distinct one-line taglines for a new coffee shop: one playful, one elegant, "
           "and one minimalist. Make each tagline its own checklist item and produce each as an "
           "artifact (work_produce_artifact). Then add a final checklist item recommending the best "
           "of the three and produce that as an artifact too. Do them one per turn, mark each done "
           "after you produce it, then call work_finish to complete this node.")


def main() -> None:
    db_path = os.path.join(tempfile.mkdtemp(prefix="work_objects_"), "work.db")
    store = WorkStore(db_path)
    wo = store.apply("create_work_object",
                     {"title": "coffee taglines", "goal_content": REQUEST, "satisfied_when_kind": "tool_success"},
                     actor="test")
    wid, goal_id = wo.id, wo.goal_node_id

    print("########## REQUEST ##########")
    print(REQUEST)
    print("#############################\n")

    run_node(store, wid, goal_id)
    status = store.load(wid).nodes[goal_id].status

    final = store.load(wid)
    subs = [n for n in final.nodes.values() if n.type == "subtask" and n.parent_id == goal_id]
    done_subs = [s for s in subs if s.status == "done"]
    arts = [n for n in final.nodes.values() if n.type == "artifact"]
    ev = [n for n in final.nodes.values() if n.type == "evidence"]

    print(f"\nnode status={status}  subtasks={len(subs)} (done={len(done_subs)})  "
          f"artifacts={len(arts)}  evidence={len(ev)}\n")

    def show(node_id, depth=0):
        n = final.nodes[node_id]
        print(f"  {'  ' * depth}- [{n.type}/{n.status}] {(n.title or '')[:56]}")
        for c in final.nodes.values():
            if c.parent_id == node_id:
                show(c.id, depth + 1)

    show(goal_id)

    print("\n=== event log (actor = who mutated) ===")
    for e in store.events(wid):
        d = e["data"]
        nid = (d.get("node_id") or d.get("id") or "")[:8]
        detail = d.get("title") or d.get("status") or d.get("relation") or ""
        print(f"  #{e['seq']:>2} {e['actor']:<22} {e['op']:<14} {nid:<8} {str(detail)[:42]}")

    print("\n=== checks ===")

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        assert ok, label

    ck("checklist reconciled to subtask nodes (>=2)", len(subs) >= 2)
    ck("planner closed subtask(s) via the checklist (>=1 done)", len(done_subs) >= 1)
    ck("the node completed", status in {"done", "satisfied", "verified", "passed"})

    store.close()
    print("\ninner loop holds: render -> planner -> reconcile -> graph, web-free.")


if __name__ == "__main__":
    main()
