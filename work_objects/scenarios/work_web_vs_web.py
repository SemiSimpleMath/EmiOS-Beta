"""work_web_manager (graph node) vs web_manager (production) on the DiCaprio task.

Counts PLANNER cycles (process_llm_result calls per planner) + wall time for each. With the proper
graph prompt (web::planner's strategy + the restored >=15 hard stop + id-checklist), work_web_manager
should resolve in ~3-6 cycles — like web_manager — not the 899s / many-cycle over-research of the old
forked prompt.

  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/work_web_vs_web.py
"""
import os
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
load_dotenv()

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import add_file_sink
from app.assistant.utils.pydantic_classes import Message
from work_objects import discharge, scope as work_scope
from work_objects.store import WorkStore
from work_objects.scope import orchestrator_scope

TASK = "Find out the age of Leonardo DiCaprio's current girlfriend."

# Cycle counter: each planner turn calls process_llm_result once. WorkPlanner calls super(), so
# patching the base Planner counts both stacks, keyed by agent name.
import app.assistant.agent_classes.Planner as PlannerMod
_orig_pllm = PlannerMod.Planner.process_llm_result
CYCLES: dict = {}


def _counting(self, result):
    CYCLES[self.name] = CYCLES.get(self.name, 0) + 1
    return _orig_pllm(self, result)


PlannerMod.Planner.process_llm_result = _counting


def _answer(result) -> str:
    data = getattr(result, "data", None) or {}
    for k in ("final_answer_answer", "final_answer", "answer"):
        if data.get(k):
            return str(data[k])[:600]
    return str(getattr(result, "content", "") or result)[:600]


def main():
    log = add_file_sink("work_web_vs_web")
    print(f"log -> {log}\n", flush=True)
    discharge._ensure_registered()
    DI.manager_registry.preload_all()

    print("=" * 80, flush=True)
    print("=== work_web_manager (graph node) ===", flush=True)
    CYCLES.clear()
    store = WorkStore(os.path.join(tempfile.mkdtemp(prefix="wwm_"), "w.db"))
    wo = store.apply("create_work_object", {"title": TASK[:50], "goal_content": TASK,
                                            "satisfied_when_kind": "tool_success"}, actor="test")
    gid = wo.goal_node_id
    t0 = time.time()
    discharge.discharge_node(store, wo.id, gid, manager_name="work_web_manager", scope_context=work_scope.orchestrator_scope(work_id=wo.id))
    dt_work = time.time() - t0
    work_cycles = CYCLES.get("work_web_manager::planner", 0)
    f = store.load(wo.id)
    g = f.nodes[gid]
    findings = [(n.content or "")[:160] for n in f.nodes.values() if n.parent_id == gid and n.type == "evidence"]
    subtasks = [f"[{n.status}] {n.title}" for n in f.nodes.values() if n.parent_id == gid and n.type == "subtask"]
    print(f"  CYCLES={work_cycles}   time={dt_work:.0f}s   status={g.status}   pod={g.pod_ref}", flush=True)
    print(f"  summary: {(g.content or '')[:300]}", flush=True)
    print(f"  subtasks ({len(subtasks)}): {subtasks}", flush=True)
    print(f"  findings ({len(findings)}): {findings}", flush=True)
    store.close()

    print("\n" + "=" * 80, flush=True)
    print("=== web_manager (production, ephemeral) ===", flush=True)
    CYCLES.clear()
    mgr = DI.multi_agent_manager_factory.create_manager("web_manager")
    msg = Message(data_type="agent_activation", sender="User", receiver="Delegator",
                  content=TASK, task=TASK, scope_context=orchestrator_scope(owner_id="jukka"))
    t0 = time.time()
    result = DI.manager_invoker.invoke(mgr, msg)
    dt_web = time.time() - t0
    web_cycles = CYCLES.get("web::planner", 0)
    print(f"  CYCLES={web_cycles}   time={dt_web:.0f}s", flush=True)
    print(f"  answer: {_answer(result)}", flush=True)

    print("\n" + "=" * 80, flush=True)
    print("=== COMPARISON ===", flush=True)
    print(f"  work_web_manager : {work_cycles} cycles   {dt_work:.0f}s", flush=True)
    print(f"  web_manager      : {web_cycles} cycles   {dt_web:.0f}s", flush=True)
    verdict = "OK (3-6 range)" if 3 <= work_cycles <= 6 else ("CLOSE" if work_cycles <= 8 else "OUT OF RANGE")
    print(f"  -> work_web_manager resolved in {work_cycles} cycles: {verdict}", flush=True)


if __name__ == "__main__":
    main()
