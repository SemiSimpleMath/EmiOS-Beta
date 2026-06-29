"""Run JUST the real orchestrator_architect on a goal and write its spawn DAG as a WorkObject
tree — the first slice of the work-tree version of WorkOrchestrator. No managers run; we only
want to see how the architect divides the problem into nodes (manager_type + depends_on).

  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/architect_worktree_smoke.py
"""
import os
os.environ["EMI_PRINT_PROMPTS"] = "1"
os.environ["EMI_PRINT_LLM_RESULTS"] = "1"

import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message
from work_objects.store import WorkStore
from work_objects.model import new_id

GOAL = ("We're hosting a small dinner for a guest who is a professional fashion model. Prepare for "
        "it: figure out good conversation topics for her, a dinner menu she'd enjoy, and a fun "
        "after-dinner activity.")


def jget(job, key, default=None):
    return job.get(key, default) if isinstance(job, dict) else getattr(job, key, default)


def main() -> None:
    bb = Blackboard()
    bb.update_state_value("task", GOAL)
    bb.update_state_value("date_time", "2026-06-18 12:00")
    bb.update_state_value("orchestrator_allowed_child_managers", ["work_web_manager"])
    bb.update_state_value("orchestrator_allowed_manager_catalog",
                          {"work_web_manager": "Researches one topic on the web and returns findings + a pod."})
    for k, v in {"orchestrator_facts_version": 0, "orchestrator_facts": "", "orchestrator_results": [],
                 "orchestrator_job_map": {}, "orchestrator_missing_requirements": [], "orchestrator_tick": 0,
                 "orchestrator_allowed_child_orchestrators": [], "information": ""}.items():
        bb.update_state_value(k, v)

    architect = DI.agent_factory.create_agent("shared::orchestrator_architect", bb)
    print("=== running the REAL orchestrator_architect on the dinner goal ===\n")
    architect.action_handler(Message(data_type="orchestrator_architecture_plan"))
    spawn = bb.get_state_value("spawn", []) or []
    notes = bb.get_state_value("notes", "")
    print(f"\n=== architect produced {len(spawn)} job(s) | notes: {notes} ===\n")

    # Map the architect's DAG (spawn) -> a WorkObject tree.
    store = WorkStore(os.path.join(tempfile.mkdtemp(prefix="work_objects_"), "w.db"))
    wo = store.apply("create_work_object", {"title": "dinner prep", "goal_content": GOAL,
                                            "satisfied_when_kind": "all_owned_children_done"}, actor="architect")
    wid, goal = wo.id, wo.goal_node_id
    store.apply("set_status", {"work_id": wid, "node_id": goal, "status": "dispatched"}, actor="architect")
    job_to_node = {}
    for job in spawn:
        nid = new_id("node")
        store.apply("add_node", {"work_id": wid, "id": nid, "type": "subtask", "parent_id": goal,
                                 "title": str(jget(job, "job_id", ""))[:60],
                                 "content": str(jget(job, "sub_task_for_manager", "")),
                                 "owner_agent": str(jget(job, "manager_type", "")),
                                 "satisfied_when_kind": "tool_success"}, actor="architect")
        job_to_node[str(jget(job, "job_id", ""))] = nid
    for job in spawn:
        jid = str(jget(job, "job_id", ""))
        for dep in (jget(job, "depends_on", []) or []):
            if str(dep) in job_to_node and jid in job_to_node:
                store.apply("add_edge", {"work_id": wid, "src": job_to_node[str(dep)],
                                         "dst": job_to_node[jid], "relation": "depends_on"}, actor="architect")

    final = store.load(wid)
    print("=== resulting WorkObject tree ===")

    def show(nid, depth=0):
        n = final.nodes[nid]
        deps = [final.nodes[e.src].title for e in final.edges if e.dst == nid and e.relation == "depends_on"]
        dtag = f"   depends_on={deps}" if deps else ""
        print(f"  {'  ' * depth}- [{n.type}/{n.status}] {(n.title or '')[:42]}"
              + (f"  (mgr={n.owner_agent})" if n.owner_agent else "") + dtag)
        if n.content:
            print(f"  {'  ' * depth}      task: {n.content[:100]}")
        for c in final.nodes.values():
            if c.parent_id == nid:
                show(c.id, depth + 1)

    show(goal)
    store.close()


if __name__ == "__main__":
    main()
