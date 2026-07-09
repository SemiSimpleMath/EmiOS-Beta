"""Cross-work-object node-id integrity (work-store audit W1, 2026-07-09).

nodes.id is the table's GLOBAL primary key, but the architect passed LLM
slugs straight through as ids — recurring nightly plans re-minted the same
slug ("book_ac_service" in 6 WOs) and INSERT OR REPLACE silently re-homed
the row to the new work object, leaving the earlier graph with dangling
children (53 live) and, when still active, permanently unwritable
(validate() raises on every subsequent apply).

Now the store refuses a caller-supplied id that lives in another work
object, and apply_architect_dag namespaces each new slug per-WO (batch-wide,
so intra-plan depends_on references keep working).
"""
from __future__ import annotations

import os
import tempfile

import pytest

from work_objects.store import WorkStore


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = WorkStore(path)
    yield s
    s.close()
    os.remove(path)


def _mk_wo(store, title: str) -> str:
    return store.apply("create_work_object", {"title": title}, actor="test").id


def test_cross_wo_id_reuse_is_refused(store):
    wo_a = _mk_wo(store, "night A")
    goal_a = store.load(wo_a).goal_node_id
    store.apply("add_node", {"work_id": wo_a, "id": "book_ac_service", "type": "subtask",
                             "parent_id": goal_a, "title": "book it"}, actor="test")

    wo_b = _mk_wo(store, "night B")
    goal_b = store.load(wo_b).goal_node_id
    with pytest.raises(ValueError, match="already belongs to work object"):
        store.apply("add_node", {"work_id": wo_b, "id": "book_ac_service", "type": "subtask",
                                 "parent_id": goal_b, "title": "book it again"}, actor="test")

    # Night A's graph is intact AND still writable.
    a = store.load(wo_a)
    assert "book_ac_service" in a.nodes
    assert a.nodes["book_ac_service"].work_id == wo_a
    store.apply("set_status", {"work_id": wo_a, "node_id": "book_ac_service",
                               "status": "actionable"}, actor="test")


def test_architect_namespaces_slugs_per_wo():
    from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = WorkStore(path)
    try:
        specs = [
            {"node_id": "gather_info", "title": "Gather info", "detail": "collect the facts"},
            {"node_id": "book_ac_service", "title": "Book AC service",
             "detail": "call them", "depends_on": ["gather_info"]},
        ]
        wo_a = _mk_wo(store, "night A")
        res_a = apply_architect_dag(store, wo_a, specs)
        wo_b = _mk_wo(store, "night B")
        res_b = apply_architect_dag(store, wo_b, specs)

        assert len(res_a["added"]) == 2 and len(res_b["added"]) == 2
        assert res_a["edges"] == 1 and res_b["edges"] == 1
        assert set(res_a["added"]).isdisjoint(set(res_b["added"]))  # distinct real ids

        # Night A survived night B's re-mint: nodes present, edge endpoints resolve,
        # and the graph is still writable (validate passes on apply).
        a = store.load(wo_a)
        assert len([n for n in a.nodes.values() if n.type == "subtask"]) == 2
        a.validate()
        store.apply("set_status", {"work_id": wo_a, "node_id": res_a["added"][0],
                                   "status": "actionable"}, actor="test")

        # Intra-plan dependency wired within each WO (the namespaced ids).
        b = store.load(wo_b)
        dep_edges = [e for e in b.edges if e.relation == "depends_on"]
        assert len(dep_edges) == 1
        assert dep_edges[0].src in res_b["added"] and dep_edges[0].dst in res_b["added"]
    finally:
        store.close()
        os.remove(path)


def test_store_singleton_survives_a_thread_race(monkeypatch, tmp_path):
    """get_dayflow_work_store used to be an unlocked check-then-set — two
    threads at first touch built two WorkStore instances with two separate
    RLocks, breaking the one-lock-serializes-all-writers guarantee
    (audit W3)."""
    import threading

    from app.assistant.dayflow_orchestrator import work_store as ws

    db = str(tmp_path / "race_work.db")
    monkeypatch.setenv("DAYFLOW_WORK_DB", db)
    ws._stores.pop(db, None)

    results, barrier = [], threading.Barrier(8)

    def _grab():
        barrier.wait()
        results.append(ws.get_dayflow_work_store())

    threads = [threading.Thread(target=_grab) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=10)

    assert len(results) == 8
    assert all(r is results[0] for r in results)  # one instance, one lock
    ws._stores.pop(db, None)
    results[0].close()


def test_busy_timeout_set_in_the_constructor(tmp_path):
    s = WorkStore(str(tmp_path / "bt.db"), busy_timeout_ms=4321)
    try:
        assert s._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 4321
    finally:
        s.close()


def test_architect_replan_is_idempotent_on_the_same_wo():
    from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = WorkStore(path)
    try:
        wo = _mk_wo(store, "plan")
        specs = [{"node_id": "step_one", "title": "Step one", "detail": "do it"}]
        first = apply_architect_dag(store, wo, specs)
        second = apply_architect_dag(store, wo, specs)   # bare slug re-emitted
        assert len(first["added"]) == 1
        assert second["added"] == []                     # maps to the same namespaced id
        subtasks = [n for n in store.load(wo).nodes.values() if n.type == "subtask"]
        assert len(subtasks) == 1
    finally:
        store.close()
        os.remove(path)
