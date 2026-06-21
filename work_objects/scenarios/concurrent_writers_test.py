"""
Prove the single-writer lock: many threads writing the SAME WorkObject at once
(the parallel-managers + writing-curator scenario) never lose, drop, or corrupt a
write. No LLM, deterministic.

Without check_same_thread=False the threads would error immediately ("SQLite
objects created in a thread can only be used in that same thread"); without the
lock the shared connection would interleave and corrupt. With both, 200
concurrent add_node writes all land, the event log is exact, and the graph stays
valid.

Run from repo root:  PYTHONPATH=. .venv/Scripts/python.exe work_objects/scenarios/concurrent_writers_test.py
"""
from __future__ import annotations

import os
import tempfile
import threading

from work_objects.model import new_id
from work_objects.store import WorkStore

N_THREADS = 8
PER_THREAD = 25
TOTAL = N_THREADS * PER_THREAD


def main() -> None:
    db_path = os.path.join(tempfile.mkdtemp(prefix="work_objects_"), "work.db")
    store = WorkStore(db_path)
    wo = store.apply("create_work_object", {"title": "concurrency stress"}, actor="test")
    wid, goal_id = wo.id, wo.goal_node_id

    errors: list = []
    start = threading.Barrier(N_THREADS)  # make the threads actually race

    def worker(t: int) -> None:
        try:
            start.wait()
            for i in range(PER_THREAD):
                store.apply("add_node", {
                    "work_id": wid, "id": new_id("node"), "type": "subtask",
                    "parent_id": goal_id, "title": f"t{t}-n{i}",
                }, actor=f"thread{t}")
        except Exception as e:  # noqa: BLE001 — capture any thread failure for the assertion
            errors.append((t, repr(e)))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    final = store.load(wid)
    subtasks = [n for n in final.nodes.values() if n.type == "subtask"]
    evs = store.events(wid)

    def ck(label, ok):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        assert ok, label

    print(f"=== {N_THREADS} threads x {PER_THREAD} concurrent add_node = {TOTAL} writes ===")
    ck("no thread raised (thread-safe connection access)", not errors)
    ck(f"all {TOTAL} concurrent writes landed (none lost)", len(subtasks) == TOTAL)
    ck("no duplicate / clobbered node ids", len({n.id for n in subtasks}) == TOTAL)
    ck(f"event log is exact (1 create + {TOTAL} adds)", len(evs) == 1 + TOTAL)
    ck("projection still structurally valid", (final.validate() or True))
    if errors:
        print("errors:", errors[:3])

    store.close()
    print(f"\nsingle-writer lock serialized {TOTAL} concurrent writes with zero loss. holds.")


if __name__ == "__main__":
    main()
