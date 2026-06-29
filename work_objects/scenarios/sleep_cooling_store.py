"""
Sleep-cooling WorkObject — built entirely through the validated writer, then
round-tripped through SQLite. Proves:
  * every mutation is an event + a projection update (atomic),
  * the projection survives a reload from a fresh connection,
  * the status state machine enforces legal transitions (and rejects illegal),
  * the authority ceiling is enforced on add_node,
  * the derived rollup flips the WorkObject to `done` once the goal is satisfied.

Run from repo root:  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/sleep_cooling_store.py
"""
from __future__ import annotations

import os
import tempfile

from work_objects.store import WorkStore


def main() -> None:
    db_path = os.path.join(tempfile.mkdtemp(prefix="work_objects_"), "work.db")
    store = WorkStore(db_path)

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        assert ok, label

    print("=== build the sleep-cooling WorkObject through the writer ===")
    wo = store.apply("create_work_object", {
        "title": "stop overnight cooling at 6 AM",
        "goal_content": "At 6:00 AM stop active cooling so the AC doesn't keep running into the morning.",
        "satisfied_when_kind": "all_owned_children_done",
    }, actor="test")
    goal_id = wo.goal_node_id

    wo = store.apply("add_node", {
        "work_id": wo.id, "type": "tool", "title": "set Nest setpoint (stop active cooling)",
        "parent_id": goal_id, "owner_agent": "home_automation",
        "satisfied_when_kind": "tool_success", "side_effect": "mutate",
        "wake_kind": "time", "wake_at": "2026-06-18T06:00:00-07:00",
        "payload": {"tool": "nest_set_mode", "args": {"mode": "eco", "target_f": 75}},
    }, actor="planner")
    tool_id = next(n.id for n in wo.nodes.values() if n.type == "tool")

    wo = store.apply("add_node", {
        "work_id": wo.id, "type": "artifact", "title": "Nest confirmation",
        "parent_id": goal_id, "pod_ref": "datapod:nest_confirmation:pending",
    }, actor="planner")
    artifact_id = next(n.id for n in wo.nodes.values() if n.type == "artifact")

    wo = store.apply("add_node", {
        "work_id": wo.id, "type": "evidence", "title": "cooling actually stopped",
        "parent_id": goal_id,
    }, actor="planner")
    evidence_id = next(n.id for n in wo.nodes.values() if n.type == "evidence")

    store.apply("add_edge", {"work_id": wo.id, "src": tool_id, "dst": artifact_id, "relation": "produces"}, actor="planner")
    store.apply("add_edge", {"work_id": wo.id, "src": tool_id, "dst": artifact_id, "relation": "depends_on"}, actor="planner")
    store.apply("add_edge", {"work_id": wo.id, "src": artifact_id, "dst": evidence_id, "relation": "depends_on"}, actor="planner")

    ck("7 mutations recorded in the event log (create + 3 nodes + 3 edges)", len(store.events(wo.id)) == 7)

    # authority ceiling: a child cannot exceed its parent's authority
    store.apply("set_status", {"work_id": wo.id, "node_id": goal_id, "status": "dispatched"})  # goal proposed -> active
    # give the tool an authority, then try to add a more-privileged child under it
    try:
        store.apply("add_node", {"work_id": wo.id, "type": "subtask", "parent_id": tool_id, "authority": 99}, actor="planner")
        # tool has no authority set (None) -> ceiling is "inherit", so this is allowed.
        # Re-test against an explicit ceiling instead:
        store.apply("add_node", {"work_id": wo.id, "type": "subtask", "title": "ceiling", "parent_id": tool_id, "authority": 5}, actor="planner")
        wo = store.load(wo.id)
        ceil_parent = next(n.id for n in wo.nodes.values() if n.title == "ceiling")
        store.apply("add_node", {"work_id": wo.id, "type": "subtask", "parent_id": ceil_parent, "authority": 50}, actor="planner")
        ck("authority ceiling enforced", False)
    except ValueError as e:
        ck(f"authority ceiling enforced ({e})", True)

    # --- reload from a FRESH connection ---
    store.close()
    store2 = WorkStore(db_path)
    wo = store2.load(wo.id)
    print("\n=== after reload from a fresh SQLite connection ===")
    ck("3 work-spine children under the goal survived", len(wo.children_of(goal_id)) >= 3)
    tool = wo.nodes[tool_id]
    ck("tool wake_at round-tripped as a tz-aware datetime", tool.wake_at is not None and tool.wake_at.tzinfo is not None)
    ck("tool payload round-tripped", tool.payload.get("args", {}).get("target_f") == 75)
    ck("WorkObject not yet done", wo.status != "done")

    # --- drive the lifecycle through the writer (transitions enforced) ---
    print("\n=== drive the lifecycle (transitions enforced) ===")
    # illegal: a spine tool cannot jump proposed -> done
    try:
        store2.apply("set_status", {"work_id": wo.id, "node_id": tool_id, "status": "done"})
        ck("illegal proposed->done rejected", False)
    except ValueError as e:
        ck(f"illegal proposed->done rejected ({e})", True)

    store2.apply("set_status", {"work_id": wo.id, "node_id": tool_id, "status": "dispatched"})
    store2.apply("set_status", {"work_id": wo.id, "node_id": tool_id, "status": "done"})
    store2.apply("set_status", {"work_id": wo.id, "node_id": artifact_id, "status": "done"})
    wo_final = store2.apply("set_status", {"work_id": wo.id, "node_id": evidence_id, "status": "verified"})

    ck("WorkObject rolled up to done once goal satisfied", wo_final.status == "done")
    ck("event log grew with the transitions", len(store2.events(wo.id)) >= 9)
    store2.close()

    print("\nround-trip + writer + transitions + ceiling + rollup all hold.")


if __name__ == "__main__":
    main()
