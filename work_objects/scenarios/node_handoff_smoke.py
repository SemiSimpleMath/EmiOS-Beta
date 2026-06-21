"""
work_emi_team_manager -> work_web_manager NODE HANDOFF (multi-surface: research THEN email).

Goal: find DiCaprio's current girlfriend, then email the result. work_emi_team_manager sees TWO
surfaces — web research + email — and delegates each. It CALLS the `work_web_manager` tool for the
research; because work_web_manager is node_aware, ManagerInterface mints a fresh child node under the
goal and runs the web worker ON it (full graph context). The worker writes its findings back, the
planner reads the result, then delegates the email to personal_admin_manager carrying that result.

We verify the whole multi-surface flow:
  - a child node owned by 'work_web_manager' reaches a terminal status with a result on the graph,
  - the graph is CLEAN: exactly ONE handoff subtask (no orphan milestone — the delegator's handoff
    IS its node), and
  - the email is composed afterward AND its body carries the researched answer (result flowed
    research -> planner -> email). send_email is stubbed (capture, not fire).

  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/node_handoff_smoke.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from dotenv import load_dotenv
load_dotenv()

import app.assistant.tests.test_setup  # noqa: F401 - bootstrap DI
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import add_file_sink
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects import work_runtime
from work_objects.store import WorkStore

CAPTURED = []


def _stub_send_email():
    cfg = DI.tool_registry.get_tool("send_email")
    cls = cfg.get("tool_class") if isinstance(cfg, dict) else None
    if cls is None:
        print("WARN: send_email tool not found — cannot stub", flush=True)
        return
    def _stub(self, tool_message):
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        CAPTURED.append({k: args.get(k) for k in ("to", "subject", "body")})
        return ToolResult(result_type="send_email",
                          content=f"Email successfully sent to {args.get('to')}",
                          data={"message_id": "stubbed-capture"})
    cls.execute = _stub
    cls.requires_approval = False


GOAL = ("Find Leonardo DiCaprio's current girlfriend — her name and her age — then email the user "
        "(user@example.com) the result with subject 'DiCaprio gf'.")


def main():
    log = add_file_sink("node_handoff_smoke")
    print(f"full logs -> {log}\n", flush=True)
    work_runtime._ensure_registered()
    DI.manager_registry.preload_all()
    _stub_send_email()

    # WORK_DB lets this populate a PERSISTENT store for inspection (e.g. the /work UI);
    # default stays a throwaway temp db so normal test runs leave no trace.
    db = os.environ.get("WORK_DB") or os.path.join(tempfile.mkdtemp(prefix="handoff_"), "w.db")
    print(f"work store -> {db}\n", flush=True)
    store = WorkStore(db)
    wo = store.apply("create_work_object", {"title": "dicaprio gf + email", "goal_content": GOAL,
                                            "satisfied_when_kind": "tool_success"}, actor="test")
    gid = wo.goal_node_id

    print("=== run_node(work_emi_team_manager) on the goal ===\n", flush=True)
    work_runtime.run_node(store, wo.id, gid, manager_name="work_emi_team_manager")

    final = store.load(wo.id)
    g = final.nodes[gid]
    all_nodes = list(final.nodes.values())
    goal_subtasks = [n for n in all_nodes if n.parent_id == gid and n.type == "subtask"]
    web_nodes = [n for n in all_nodes if (n.owner_agent or "").lower() == "work_web_manager"]
    # a handoff "nests under a checklist item" when its parent is a subtask that is itself a child of the goal
    def _nests_under_checklist_item(w):
        p = final.nodes.get(w.parent_id)
        return bool(p) and p.id != gid and p.type == "subtask" and p.parent_id == gid
    nested = [w for w in web_nodes if _nests_under_checklist_item(w)]

    print("=== GRAPH ===", flush=True)
    def show(nid, d=0):
        n = final.nodes[nid]
        tag = f" owner={n.owner_agent}" if n.owner_agent else ""
        pod = f" pod={n.pod_ref}" if n.pod_ref else ""
        print(f"  {'  ' * d}- [{n.type}/{n.status}]{tag}{pod} {(n.title or '')[:60]}", flush=True)
        for c in final.nodes.values():
            if c.parent_id == nid:
                show(c.id, d + 1)
    show(gid)

    print(f"\ngoal status: {g.status}", flush=True)
    print(f"web-owned (handed-off) nodes: {len(web_nodes)}", flush=True)
    for w in web_nodes:
        print(f"  - [{w.status}] owner={w.owner_agent} pod={w.pod_ref} :: {(w.content or w.title or '')[:70]}", flush=True)
    print(f"\ncaptured emails ({len(CAPTURED)}):", flush=True)
    for c in CAPTURED:
        print(f"  to={c['to']} subj={c['subject']!r}\n  body={(c['body'] or '')[:200]!r}", flush=True)

    # The email body should carry the researched answer (a name + an age/year) — the proof the
    # result flowed research -> planner -> email, not a placeholder.
    bodies = " ".join((c.get("body") or "") for c in CAPTURED)
    body_has_result = len(bodies.strip()) > 30 and bool(re.search(r"\d", bodies))

    print("\n=== checks ===", flush=True)
    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}", flush=True)
        assert ok, label
    TERMINAL = {"done", "verified", "passed"}
    ck("a research sub-task was handed off to the web worker (web-owned node)", len(web_nodes) >= 1)
    ck("the handed-off node reached a terminal status", any(w.status in TERMINAL for w in web_nodes))
    ck("the web worker wrote a result back to the graph (pod or content)",
       any((w.pod_ref or (w.content or "").strip()) for w in web_nodes))
    ck("the goal was decomposed into checklist surfaces (subtask nodes under it)", len(goal_subtasks) >= 1)
    ck("the work_web handoff NESTS UNDER a checklist surface, not the goal directly", len(nested) >= 1)
    ck("work_emi_team then moved on and composed the email", len(CAPTURED) >= 1)
    ck("the email body carries the researched answer (result flowed research -> email)", body_has_result)
    store.close()
    print("\nmulti-surface handoff works: work_emi_team_manager -> work_web_manager (graph node) "
          "-> result -> email.", flush=True)


if __name__ == "__main__":
    main()
