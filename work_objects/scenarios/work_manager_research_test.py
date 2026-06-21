"""
WORK-MANAGER research test — the work-manager analogue of a web_manager test, run
INDEPENDENT of the orchestrator. Give the work manager ONE multi-part research node
and verify the inner loop edits the graph properly:
  - it creates id'd subtask nodes (its checklist / checkpoints), with no duplicate churn,
  - closed checkpoints carry a closing evidence note (on the node),
  - the task node carries the research pod (surfaced by the final answer) as its outcome,
  - the node completes.

Mirrors manager_tests/* (single task -> manager -> assert). Uses the work_web_manager
(worker_agents::web_planner researches directly via search_web/scrape_url and writes the
graph) — needs GOOGLE_SEARCH_API_KEY; loaded by runtime_setup.

  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/work_manager_research_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Standing requirement: every WorkObject scenario run prints the FULL rendered system + user prompt
# for EVERY agent (EMI_PRINT_PROMPTS) plus each agent's output (EMI_PRINT_LLM_RESULTS) — so we never
# debug blind. Both are read at LLM-call time, so setting them here covers the whole run.
os.environ["EMI_PRINT_PROMPTS"] = "1"
os.environ["EMI_PRINT_LLM_RESULTS"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401 - bootstrap DI

from work_objects.store import WorkStore
from work_objects.work_runtime import run_node

NODE = ("Research the Gaggia Classic Pro espresso machine for a prospective buyer: (1) its current "
        "US price, (2) key specs (boiler type, portafilter size, whether it has a PID), and (3) its "
        "main pros and cons.")


def main() -> None:
    db = os.path.join(tempfile.mkdtemp(prefix="work_objects_"), "w.db")
    store = WorkStore(db)
    # One research node, owned by the work manager (no orchestrator).
    wo = store.apply("create_work_object", {"title": "espresso research", "goal_content": NODE,
                                            "satisfied_when_kind": "tool_success"}, actor="test")
    wid, node_id = wo.id, wo.goal_node_id

    print("=== work-manager research test (single node, no orchestrator) ===\n")
    run_node(store, wid, node_id, manager_name="work_web_manager")
    status = store.load(wid).nodes[node_id].status

    final = store.load(wid)
    goal = final.nodes[node_id]
    subs = [n for n in final.nodes.values() if n.type == "subtask"]
    done_subs = [s for s in subs if s.status == "done"]
    subs_with_evidence = [s for s in subs if (s.content or "").strip()]

    print(f"\nnode status={status}  outcome pod={goal.pod_ref}\n"
          f"subtasks={len(subs)} (done={len(done_subs)}, with evidence note={len(subs_with_evidence)})\n")

    def show(nid, depth=0):
        n = final.nodes[nid]
        detail = (n.pod_ref or n.content or "")
        extra = f"  -> {detail[:70]}" if detail else ""
        print(f"  {'  ' * depth}- [{n.type}/{n.status}] {(n.title or '')[:50]}{extra}")
        for c in final.nodes.values():
            if c.parent_id == nid:
                show(c.id, depth + 1)

    show(node_id)

    print("\n=== checks (the work-manager sub-tests) ===")

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        assert ok, label

    ck("created subtask nodes (checklist / checkpoints)", len(subs) >= 1)
    ck("checklist stayed clean — no duplicate-node churn", len(subs) <= 8)
    ck("closed checkpoints carry an evidence note", len(subs_with_evidence) >= 1)
    ck("task node carries the research pod as its outcome", bool((goal.pod_ref or "").strip()))
    ck("the node completed", status in {"done", "satisfied", "verified", "passed"})

    store.close()
    print("\nwork manager edited the graph properly: id'd subtasks + closing evidence notes + outcome pod + done.")


if __name__ == "__main__":
    main()
