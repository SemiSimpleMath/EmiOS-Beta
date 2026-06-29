"""
Scheduler hot path — prove the park/wake + dependency gating that dayflow will lean on
once plans live in the work graph instead of unified_log (whose 24h freshness window
means a wait >24h never wakes). No LLM, deterministic. Two properties:

  1. TIME PARK/WAKE: a node parked wake_at=tomorrow is ABSENT from ready_nodes(now) and
     PRESENT from ready_nodes(now+2d) — the multi-day wait unified_log can't do.
  2. DEPENDENCY GATE: a node whose depends_on is unsatisfied stays out of the ready-set
     until that dependency is satisfied, then enters it.

Run from repo root:  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/scheduler_ready_set_test.py
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta

from work_objects.model import utcnow
from work_objects.store import WorkStore


def main() -> None:
    db = os.path.join(tempfile.mkdtemp(prefix="work_objects_"), "w.db")
    store = WorkStore(db)

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        assert ok, label

    now = utcnow()
    in_two_days = now + timedelta(days=2)

    # ---------------- 1. TIME PARK / WAKE ----------------
    print("=== 1. time park/wake (the multi-day wait unified_log's 24h window can't do) ===")
    wo = store.apply("create_work_object", {
        "title": "park/wake", "goal_content": "two steps, second parked one day out",
        "satisfied_when_kind": "all_owned_children_done",
    }, actor="test")
    goal_id = wo.goal_node_id
    store.apply("set_status", {"work_id": wo.id, "node_id": goal_id, "status": "dispatched"})

    store.apply("add_node", {"work_id": wo.id, "type": "subtask", "title": "do now",
                             "parent_id": goal_id}, actor="planner")
    store.apply("add_node", {"work_id": wo.id, "type": "subtask", "title": "do tomorrow",
                             "parent_id": goal_id, "wake_kind": "time",
                             "wake_at": (now + timedelta(days=1)).isoformat()}, actor="planner")
    wo = store.load(wo.id)

    ready_now = {n.title for n in wo.ready_nodes(now)}
    ready_later = {n.title for n in wo.ready_nodes(in_two_days)}
    ck("immediately-runnable node is ready now", "do now" in ready_now)
    ck("parked node is NOT ready now (wake_at tomorrow > now)", "do tomorrow" not in ready_now)
    ck("parked node BECOMES ready once now passes wake_at", "do tomorrow" in ready_later)

    # ---------------- 2. DEPENDENCY GATE ----------------
    print("\n=== 2. dependency gate (a node waits for its upstream) ===")
    wo2 = store.apply("create_work_object", {
        "title": "dep gate", "goal_content": "B depends on A",
        "satisfied_when_kind": "all_owned_children_done",
    }, actor="test")
    g2 = wo2.goal_node_id
    store.apply("set_status", {"work_id": wo2.id, "node_id": g2, "status": "dispatched"})
    store.apply("add_node", {"work_id": wo2.id, "type": "subtask", "title": "A (upstream)",
                             "parent_id": g2}, actor="planner")
    store.apply("add_node", {"work_id": wo2.id, "type": "subtask", "title": "B (downstream)",
                             "parent_id": g2}, actor="planner")
    wo2 = store.load(wo2.id)
    a = next(n for n in wo2.nodes.values() if n.title == "A (upstream)")
    b = next(n for n in wo2.nodes.values() if n.title == "B (downstream)")
    # edge src=A dst=B relation=depends_on  =>  deps_of(B) == [A]  (B depends on A)
    store.apply("add_edge", {"work_id": wo2.id, "src": a.id, "dst": b.id,
                             "relation": "depends_on"}, actor="planner")

    wo2 = store.load(wo2.id)
    ready = {n.id for n in wo2.ready_nodes(now)}
    ck("A (no deps) is ready", a.id in ready)
    ck("B is BLOCKED while A is unsatisfied", b.id not in ready)

    # satisfy A: spine node steps proposed -> active -> done
    store.apply("set_status", {"work_id": wo2.id, "node_id": a.id, "status": "dispatched"})
    store.apply("set_status", {"work_id": wo2.id, "node_id": a.id, "status": "done"})
    wo2 = store.load(wo2.id)
    ready_after = {n.id for n in wo2.ready_nodes(now)}
    ck("B BECOMES ready once A is done", b.id in ready_after)

    store.close()
    print("\nscheduler park/wake + dependency gating hold — the multi-day plan substrate is sound.")


if __name__ == "__main__":
    main()
