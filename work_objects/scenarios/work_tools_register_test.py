"""
Prove the WorkGraph ops work as REAL registered BaseTools, dispatched through the
live tool contract (ToolMessage -> ToolResult), mutating the graph via the
runtime context bridge. No LLM — this is the deterministic foundation the agent
loop (next brick) stands on.

This is the first app.* import, so it bootstraps DI first.

Run from repo root:  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/work_tools_register_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401  — bootstrap DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolMessage

from work_objects.runtime import reset_work_context, set_work_context
from work_objects.store import WorkStore
from work_objects.work_tools import register_work_tools


def call(name: str, arguments: dict):
    """Invoke a registered work tool the way the tool_caller does: instantiate its
    tool_class and execute a ToolMessage carrying {tool_name, arguments}."""
    cls = DI.tool_registry.registry[name]["tool_class"]
    tm = ToolMessage(tool_name=name, tool_data={"tool_name": name, "arguments": arguments})
    return cls().execute(tm)


def main() -> None:
    db_path = os.path.join(tempfile.mkdtemp(prefix="work_objects_"), "work.db")
    store = WorkStore(db_path)
    wo = store.apply("create_work_object", {
        "title": "research the effects of X", "goal_content": "Find what X does.",
    }, actor="test")
    goal_id = wo.goal_node_id

    names = register_work_tools(DI.tool_registry)

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        assert ok, label

    print("=== checks ===")
    ck(f"registered {len(names)} work tools", len(names) == 10)
    ck("tools resolve through the live registry (get_tool)",
       DI.tool_registry.get_tool("work_add_subtask") is not None
       and DI.tool_registry.get_tool("work_finish") is not None)
    ck("registry entry has the live tool_args shape (args + arguments models)",
       set(DI.tool_registry.registry["work_record_finding"]["tool_args"]) == {"args", "arguments"})

    # --- the goal's agent decomposes via the tool ---
    tok = set_work_context(store, wo.id, goal_id, actor="planner")
    res = call("work_add_subtask", {"title": "search the web for X", "satisfied_when_kind": "tool_success"})
    reset_work_context(tok)
    ck("work_add_subtask returned a ToolResult with the new node id", bool(res.data.get("node_id")))
    sub_id = res.data["node_id"]
    g = store.load(wo.id)
    ck("subtask exists in the graph, owned by the goal", g.nodes[sub_id].parent_id == goal_id)

    # --- the subtask's agent records a finding + an artifact, then finishes ---
    # (the WorkManager normally picks up the node proposed->active before running
    # its agent; this test bypasses the manager, so simulate that pickup.)
    store.apply("set_status", {"work_id": wo.id, "node_id": sub_id, "status": "dispatched"}, actor="manager")
    tok = set_work_context(store, wo.id, sub_id, actor="web_researcher")
    fres = call("work_record_finding", {"claim": "X increases Y by ~30%", "confidence": 0.8, "pod_ref": "datapod:x:aaa"})
    call("work_produce_artifact", {"title": "notes on X", "pod_ref": "datapod:x:aaa"})
    call("work_finish", {"status": "satisfied"})
    reset_work_context(tok)

    g = store.load(wo.id)
    ev = g.nodes[fres.data["node_id"]]
    ck("finding minted as Evidence under the subtask", ev.type == "evidence" and ev.parent_id == sub_id)
    ck("finding kept confidence in payload", ev.payload.get("confidence") == 0.8)
    ck("produces edge wired subtask -> finding",
       any(e.relation == "produces" and e.src == sub_id and e.dst == ev.id for e in g.edges))
    ck("work_finish closed the subtask (active -> done)", g.nodes[sub_id].status == "done")

    # --- a read tool returns graph state through the same contract ---
    tok = set_work_context(store, wo.id, goal_id, actor="planner")
    sres = call("work_graph_summary", {})
    reset_work_context(tok)
    ck("work_graph_summary read the graph through the tool contract",
       isinstance(sres.data.get("nodes"), list) and len(sres.data["nodes"]) >= 3)

    store.close()
    print("\nWorkGraph ops are live registered BaseTools, mutating the graph via the context bridge. holds.")


if __name__ == "__main__":
    main()
