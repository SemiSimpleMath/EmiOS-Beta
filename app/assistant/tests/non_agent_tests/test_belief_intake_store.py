"""belief_engine.intake.store — the intake's tables behave the same prefixed (app DB) and bare (replay).
No model calls; invented atoms and verdicts only."""
import sqlite3

import pytest

from belief_engine.intake.store import IntakeStore, sqlite_file


def atom(statement, *texts, kind="stable_preference"):
    return {"statement": statement, "kind": kind, "scope": "chronic",
            "sources": [{"time": f"2026-01-01 0{i}:00", "kind": "said", "relation": "support", "text": t,
                         "source_ref": f"message:{i}"} for i, t in enumerate(texts, 1)]}


@pytest.fixture(params=["", "belief_intake_"])
def store(tmp_path, request):
    return IntakeStore(sqlite_file(tmp_path / "s.db"), request.param)


def test_new_belief_keeps_every_source(store):
    bid = store.apply("2026-01-01", atom("A likes tea", "i like tea", "tea is great"), [1.0, 0.0], {"verdict": "new"})
    assert bid == "B1"
    b = store.beliefs()[0]
    assert b["statement"] == "A likes tea" and b["parent_id"] is None
    assert [s["text"] for s in b["sources"]] == ["i like tea", "tea is great"]


def test_same_attaches_only_bearing_sources(store):
    store.apply("2026-01-01", atom("A likes tea", "i like tea"), [1.0, 0.0], {"verdict": "new"})
    store.apply("2026-01-02", atom("A enjoys tea", "took the dogs out", "love a cup of tea"), [1.0, 0.0],
                {"verdict": "same", "target": "B1", "sources": [2], "reasoning": "same claim"})
    b = store.beliefs()[0]
    assert [s["text"] for s in b["sources"]] == ["i like tea", "love a cup of tea"]
    assert len(store.beliefs()) == 1


def test_contradicts_marks_relation_and_revise_keeps_history(store):
    store.apply("2026-01-01", atom("A likes tea", "i like tea"), [1.0, 0.0], {"verdict": "new"})
    store.apply("2026-01-03", atom("A stopped drinking tea", "no more tea for me"), [0.0, 1.0],
                {"verdict": "contradicts", "target": "B1", "sources": [1], "reasoning": "cannot both hold"})
    store.revise("B1", "2026-01-03", {"statement": "A stopped drinking tea; until 2026-01-03, A liked tea.",
                                      "kind": "stable_preference", "reasoning": "later words"}, [0.0, 1.0])
    b = store.beliefs()[0]
    assert b["statement"].startswith("A stopped drinking tea")
    assert [s["relation"] for s in b["sources"]] == ["support", "contradict"]
    conn = sqlite3.connect(str(store_path(store)))
    p = store.p
    old, new = conn.execute(f"SELECT old_statement, new_statement FROM {p}revisions").fetchone()
    assert old == "A likes tea" and new.startswith("A stopped")


def test_refines_is_one_level(store):
    store.apply("2026-01-01", atom("A likes tea", "i like tea"), [1.0, 0.0], {"verdict": "new"})
    store.apply("2026-01-02", atom("A likes tea hot", "hot tea only"), [1.0, 0.1], {"verdict": "refines", "target": "B1"})
    store.apply("2026-01-03", atom("A likes tea very hot", "scalding"), [1.0, 0.2], {"verdict": "refines", "target": "B2"})
    parents = {b["id"]: b["parent_id"] for b in store.beliefs()}
    assert parents == {"B1": None, "B2": "B1", "B3": "B1"}


def test_days_done_counts_only_done(store):
    store.mark_day("2026-01-01", "done", 3)
    store.mark_day("2026-01-02", "failed", 0, "ValueError: x")
    assert store.days_done() == {"2026-01-01"}
    store.mark_day("2026-01-02", "done", 2)
    assert store.days_done() == {"2026-01-01", "2026-01-02"}


def store_path(store):
    # The sqlite_file provider keeps its connection in the closure; read its file path back.
    with store._connect(False) as c:
        return c.execute("PRAGMA database_list").fetchone()[2]


def test_ids_never_reuse_a_removed_number(store):
    store.apply("2026-01-01", atom("A likes tea", "t"), [1.0, 0.0], {"verdict": "new"})
    store.apply("2026-01-01", atom("A likes coffee", "c"), [0.0, 1.0], {"verdict": "new"})
    with store._connect(True) as c:
        c.execute(f"DELETE FROM {store.p}beliefs WHERE id='B1'")
    assert store.apply("2026-01-02", atom("A likes cocoa", "k"), [0.5, 0.5], {"verdict": "new"}) == "B3"


def test_retired_belief_leaves_the_active_set_but_keeps_history(store):
    store.apply("2026-01-01", atom("A wants a physical scheduled", "remind me"), [1.0, 0.0], {"verdict": "new"})
    store.add_evidence("B1", "2026-02-01", kind="did", relation="support", text="booked",
                       source_ref="work:w1", via="work_outcome")
    assert store.has_evidence("B1", "work:w1") and not store.has_evidence("B1", "work:w2")
    store.retire("B1", "2026-02-01", "the physical was booked")
    assert store.beliefs() == []
    kept = store.beliefs(include_retired=True)[0]
    assert kept["status"] == "retired" and len(kept["sources"]) == 2


def test_merge_moves_every_source_and_retires_the_duplicate(store):
    store.apply("2026-01-01", atom("A likes brie with crackers", "brie is the best"), [1.0, 0.0], {"verdict": "new"})
    store.apply("2026-01-02", atom("A considers brie top tier", "brie top tier", "love brie"), [1.0, 0.0],
                {"verdict": "new"})
    assert store.merge("B2", "B1", "2026-01-03", "same claim") == 2
    [b] = store.beliefs()
    assert b["id"] == "B1" and [s["text"] for s in b["sources"]] == ["brie is the best", "brie top tier", "love brie"]
    assert store.get("B2")["status"] == "retired"
    with pytest.raises(ValueError):
        store.merge("B2", "B1", "2026-01-04", "again")          # a retired belief cannot merge again
    with pytest.raises(ValueError):
        store.merge("B1", "B1", "2026-01-04", "itself")


def test_split_keeps_the_main_claim_on_its_id_and_each_part_rests_on_the_evidence(store):
    store.apply("2026-01-01", atom("A wants 70F at night and 75F by morning", "70 at night", "75 by morning"),
                [1.0, 0.0], {"verdict": "new"})
    assert store.split("B1", "2026-01-02", ["A wants 70F at night", "A wants 75F by morning"],
                       [[1.0, 0.0], [0.0, 1.0]], "two claims") == ["B2"]
    b1, b2 = store.beliefs()
    assert (b1["id"], b1["statement"]) == ("B1", "A wants 70F at night")
    assert (b2["id"], b2["statement"], b2["kind"]) == ("B2", "A wants 75F by morning", "stable_preference")
    assert [s["text"] for s in b2["sources"]] == ["70 at night", "75 by morning"]
    conn = sqlite3.connect(str(store_path(store)))
    reasons = [r[0] for r in conn.execute(f"SELECT reasoning FROM {store.p}revisions ORDER BY id")]
    assert reasons == ["split from B1: two claims", "split into B2: two claims"]
    with pytest.raises(ValueError):
        store.split("B1", "2026-01-03", ["only one"], [[1.0, 0.0]], "nothing to split")
    store.retire("B2", "2026-01-03", "gone")
    with pytest.raises(ValueError):
        store.split("B2", "2026-01-03", ["x", "y"], [[1.0, 0.0], [0.0, 1.0]], "retired")
