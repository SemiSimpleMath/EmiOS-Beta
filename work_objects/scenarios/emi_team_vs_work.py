"""
Head-to-head: emi_team (transient) vs work_manager (graph) on 4 real tasks.

The WRITE tools are STUBBED to CAPTURE the intended action without firing it — no real
email/todo, no approval hang. ask_user is stubbed non-blocking. Read-only work (web, KG)
runs live. Both managers run at authority 99 (orchestrator_scope) so no approval gate.

For each task we print: emi_team's answer (transient) vs the work_manager's GRAPH
(findings + status + summary), plus wall time, plus every captured write.

  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/emi_team_vs_work.py
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

import app.assistant.tests.test_setup  # noqa: F401 - bootstrap DI
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import add_file_sink
from app.assistant.utils.pydantic_classes import Message, ToolResult
from work_objects import discharge
from work_objects.scenarios._scenario_scope import scenario_scope
from work_objects.store import WorkStore
from work_objects.scenarios._scenario_scope import scenario_scope

CAPTURED: list = []          # every stubbed write, tagged by which manager attempted it
_CURRENT = {"by": "?"}       # which manager is running right now


def _install_stubs() -> None:
    """Patch the REGISTRY's tool classes (the exact objects tool_caller instantiates via
    tool_config['tool_class'] — NOT the freshly-imported ones, which are different objects).
    Capture-not-fire for the writes; a non-blocking, KG-first reply for ask_user so any agent
    that still carries it (e.g. bash_team) can't block on the missing reply_router."""
    reg = DI.tool_registry

    def _cls(name):
        cfg = reg.get_tool(name)
        return cfg.get("tool_class") if isinstance(cfg, dict) else None

    send_cls = _cls("send_email")
    if send_cls is not None:
        def _stub_send(self, tool_message):
            args = (tool_message.tool_data or {}).get("arguments", {}) or {}
            CAPTURED.append({"by": _CURRENT["by"], "tool": "send_email",
                             "args": {k: args.get(k) for k in ("to", "subject", "body", "from_account")}})
            # Mimic the REAL tool's success message verbatim — a "not sent" reply makes the agent
            # think the send failed and retry in an infinite loop. We still record it in CAPTURED.
            return ToolResult(result_type="send_email",
                              content=f"Email successfully sent to {args.get('to')}",
                              data={"message_id": "stubbed-capture"})
        send_cls.execute = _stub_send
        send_cls.requires_approval = False

    todo_cls = _cls("create_todo_task")
    if todo_cls is not None:
        _orig_todo = todo_cls.execute
        _WRITE_OPS = {"add_task", "create_todo_task", "update_todo_task", "delete_todo_task", "create_tasklist"}
        def _stub_todo(self, tool_message):
            name = tool_message.tool_name or (tool_message.tool_data or {}).get("tool_name") or ""
            if name in _WRITE_OPS:
                args = (tool_message.tool_data or {}).get("arguments", {}) or {}
                CAPTURED.append({"by": _CURRENT["by"], "tool": name, "args": dict(args)})
                # Mimic the REAL tool's success message so the agent doesn't retry-loop.
                _title = args.get("task_name") or args.get("title") or "task"
                return ToolResult(result_type="success",
                                  content=f"Task '{_title}' created successfully.",
                                  data_list=[{"task_id": "stubbed-capture"}])
            return _orig_todo(self, tool_message)   # reads (get_todo_tasks) pass through live
        todo_cls.execute = _stub_todo

    ask_cls = _cls("ask_user")
    if ask_cls is not None:
        def _stub_ask_user(self, tool_message):
            args = (tool_message.tool_data or {}).get("arguments", {}) or {}
            return ToolResult(result_type="ask_user",
                              content=("No user is available to answer. Do NOT wait for a user — look the "
                                       "information up yourself (e.g. the knowledge graph) and proceed with "
                                       "your best effort, stating any assumption you made."),
                              data={"asked": args.get("text")})
        ask_cls.execute = _stub_ask_user

    print(f"  stubs installed (registry classes): send_email={send_cls is not None} "
          f"todo={todo_cls is not None} ask_user={ask_cls is not None}", flush=True)


def _probe_kg() -> None:
    print("=== KG probe (deterministic label search) ===", flush=True)
    try:
        from app.models.base import get_session
        from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
        s = get_session()
        try:
            for label in ("phil", "friday night", "meats"):
                rows = s.query(Node).filter(Node.label.ilike(f"%{label}%")).limit(6).all()
                print(f"  '{label}': {len(rows)} -> {[getattr(r, 'label', '?') for r in rows]}", flush=True)
        finally:
            s.close()
    except Exception as e:
        print(f"  probe unavailable ({type(e).__name__}: {e}) — observe from the runs", flush=True)
    print(flush=True)


TASKS = [
    "Find out the age of Leonardo DiCaprio's current girlfriend.",
    "Email me a friend's phone number.",
    "Create a todo task to get the brakes checked in the car.",
    "Find the email addresses of the likely participants of Friday Night Meats.",
]


def _answer_of(result) -> str:
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        for k in ("final_answer_answer", "final_answer", "answer"):
            if data.get(k):
                return str(data[k])
    return str(getattr(result, "content", None) or result)


def _run_emi(task: str):
    _CURRENT["by"] = "emi"
    mgr = DI.multi_agent_manager_factory.create_manager("emi_team_manager")
    msg = Message(data_type="agent_activation", sender="User", receiver="Delegator",
                  content=task, task=task, scope_context=scenario_scope(owner_id="jukka"))
    t0 = time.time()
    result = DI.manager_invoker.invoke(mgr, msg)
    return _answer_of(result), time.time() - t0


def _run_work(task: str):
    _CURRENT["by"] = "work"
    store = WorkStore(os.path.join(tempfile.mkdtemp(prefix="h2h_"), "w.db"))
    wo = store.apply("create_work_object", {"title": task[:60], "goal_content": task,
                                            "satisfied_when_kind": "tool_success"}, actor="test")
    gid = wo.goal_node_id
    t0 = time.time()
    discharge.discharge_node(store, wo.id, gid, manager_name="work_emi_team_manager", scope_context=scenario_scope(work_id=wo.id))
    dt = time.time() - t0
    f = store.load(wo.id)
    g = f.nodes[gid]
    out = {
        "status": g.status,
        "summary": (g.content or "")[:300],
        "findings": [(n.content or "")[:240] for n in f.nodes.values()
                     if n.parent_id == gid and n.type == "evidence"],
        "subtasks": [f"[{n.status}] {n.title}" for n in f.nodes.values()
                     if n.parent_id == gid and n.type == "subtask"],
        "pod": g.pod_ref,
    }
    store.close()
    return out, dt


def main() -> None:
    log = add_file_sink("emi_team_vs_work")
    print(f"full logs -> {log}\n", flush=True)
    discharge._ensure_registered()
    DI.manager_registry.preload_all()
    _install_stubs()
    _probe_kg()

    for i, task in enumerate(TASKS, 1):
        print("=" * 92, flush=True)
        print(f"TASK {i}: {task}", flush=True)
        print("=" * 92, flush=True)

        try:
            ans, dt = _run_emi(task)
            print(f"\n--- emi_team (transient)  [{dt:.0f}s] ---", flush=True)
            print((ans or "(no answer)")[:1200], flush=True)
        except Exception as e:
            print(f"\n--- emi_team ERROR: {type(e).__name__}: {e} ---", flush=True)
            traceback.print_exc()

        try:
            out, dt = _run_work(task)
            print(f"\n--- work_manager (graph)  [{dt:.0f}s]  status={out['status']} ---", flush=True)
            print(f"summary: {out['summary']}", flush=True)
            print(f"findings ({len(out['findings'])}):", flush=True)
            for fd in out["findings"]:
                print(f"  - {fd}", flush=True)
            if out["subtasks"]:
                print(f"subtasks: {out['subtasks']}", flush=True)
            if out["pod"]:
                print(f"pod: {out['pod']}", flush=True)
        except Exception as e:
            print(f"\n--- work_manager ERROR: {type(e).__name__}: {e} ---", flush=True)
            traceback.print_exc()
        print(flush=True)

    print("=" * 92, flush=True)
    print("CAPTURED WRITES (stubbed — nothing actually fired):", flush=True)
    if not CAPTURED:
        print("  (none — neither manager attempted a write)", flush=True)
    for c in CAPTURED:
        print(f"  [{c['by']}] {c['tool']}: {c['args']}", flush=True)


if __name__ == "__main__":
    main()
