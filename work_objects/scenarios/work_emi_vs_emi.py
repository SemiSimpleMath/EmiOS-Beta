"""work_emi_team_manager vs emi_team_manager on the DiCaprio task — measures TASK SPLITTING.

"Find DiCaprio's girlfriend's age" is a SINGLE surface (web research), so both should delegate to the
web team ONCE. The OLD forked work planner ("decompose into separable parts") split it into 2-3 research
sub-tasks each hitting work_web_manager; the NEW prompt ("decompose ONLY across surfaces") should match
emi_team = 1.

Counts manager-tool calls by name (the handoff + every delegation go through ManagerInterface.execute).

  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/work_emi_vs_emi.py
"""
import os
import sys
import time
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
load_dotenv()

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import add_file_sink
from app.assistant.utils.pydantic_classes import Message
from work_objects import discharge
from work_objects.scenarios._scenario_scope import scenario_scope
from work_objects.store import WorkStore
from work_objects.scenarios._scenario_scope import scenario_scope

TASK = "Find out the age of Leonardo DiCaprio's current girlfriend."

import app.assistant.lib.core_tools.manager_interface.manager_interface as MI
_orig_exec = MI.ManagerInterface.execute
CALLS: dict = {}


def _counting(self, tool_message):
    CALLS[self.manager_name] = CALLS.get(self.manager_name, 0) + 1
    return _orig_exec(self, tool_message)


MI.ManagerInterface.execute = _counting


def _answer(result) -> str:
    data = getattr(result, "data", None) or {}
    for k in ("final_answer_answer", "final_answer", "answer"):
        if data.get(k):
            return str(data[k])[:500]
    return str(getattr(result, "content", "") or result)[:500]


def main():
    log = add_file_sink("work_emi_vs_emi")
    print(f"log -> {log}\n", flush=True)
    discharge._ensure_registered()
    DI.manager_registry.preload_all()

    print("=" * 80 + "\n=== work_emi_team_manager (graph) ===", flush=True)
    CALLS.clear()
    store = WorkStore(os.path.join(tempfile.mkdtemp(prefix="wet_"), "w.db"))
    wo = store.apply("create_work_object", {"title": TASK[:50], "goal_content": TASK,
                                            "satisfied_when_kind": "tool_success"}, actor="test")
    gid = wo.goal_node_id
    t0 = time.time()
    try:
        discharge.discharge_node(store, wo.id, gid, manager_name="work_emi_team_manager", scope_context=scenario_scope(work_id=wo.id))
    except Exception as e:
        print(f"  ERROR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
    dt_work = time.time() - t0
    handoffs = CALLS.get("work_web_manager", 0)
    final = store.load(wo.id)
    g = final.nodes[gid]
    milestones = [n for n in final.nodes.values() if n.parent_id == gid and n.type == "subtask"]
    print(f"  time={dt_work:.0f}s   status={g.status}", flush=True)
    print(f"  >>> work_web_manager handoffs (research splits): {handoffs}", flush=True)
    print(f"  milestones (top-level subtasks): {len(milestones)}", flush=True)
    print(f"  all manager calls: {CALLS}", flush=True)

    def show(nid, d=0):
        n = final.nodes[nid]
        print(f"    {'  ' * d}- [{n.type}/{n.status}] owner={n.owner_agent} {(n.title or '')[:55]}", flush=True)
        for c in final.nodes.values():
            if c.parent_id == nid:
                show(c.id, d + 1)
    print("  graph:", flush=True)
    show(gid)
    store.close()
    work_handoffs = handoffs

    print("\n" + "=" * 80 + "\n=== emi_team_manager (production) ===", flush=True)
    CALLS.clear()
    mgr = DI.multi_agent_manager_factory.create_manager("emi_team_manager")
    msg = Message(data_type="agent_activation", sender="User", receiver="Delegator",
                  content=TASK, task=TASK, scope_context=scenario_scope(owner_id="jukka"))
    t0 = time.time()
    result = DI.manager_invoker.invoke(mgr, msg)
    dt_emi = time.time() - t0
    web_calls = CALLS.get("web_manager", 0)
    print(f"  time={dt_emi:.0f}s", flush=True)
    print(f"  >>> web_manager calls: {web_calls}", flush=True)
    print(f"  all manager calls: {CALLS}", flush=True)
    print(f"  answer: {_answer(result)}", flush=True)

    print("\n" + "=" * 80 + "\n=== COMPARISON (task splitting) ===", flush=True)
    print(f"  work_emi_team_manager -> work_web_manager handoffs : {work_handoffs}", flush=True)
    print(f"  emi_team_manager      -> web_manager calls         : {web_calls}", flush=True)
    print(f"  -> {'MATCH — no over-split' if work_handoffs == web_calls else f'work_emi_team split {work_handoffs}x vs emi_team {web_calls}x'}",
          flush=True)


if __name__ == "__main__":
    main()
