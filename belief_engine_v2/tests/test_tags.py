"""Tag vocab enforcement (anti-proliferation) + retrieval tag-scoping."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

from belief_engine_v2 import tags as tm
from belief_engine_v2.retrieval import beliefs_for_context
from belief_engine_v2.store import Claim, Store


def _store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.remove(path)
    return Store(path)


def _add(store, obj, statement, occurred):
    store.append(Claim(subject="s", predicate="p", object=obj, statement_nl=statement),
                 source="seed", polarity="affirm", occurred_at=occurred, recorded_at=occurred)


def test_sanitize_enforces_vocab():
    # off-vocab dropped; lowercased, trimmed, deduped — the anti-proliferation guarantee.
    assert tm.sanitize(["meal", " FOOD ", "not_a_tag", "meal"]) == ["meal", "food"]
    assert tm.sanitize([]) == []
    assert "meal" in tm.valid_tags() and "not_a_tag" not in tm.valid_tags()


def test_pull_set_is_validated():
    ps = tm.pull_set("meal_engine")
    assert "meal" in ps and "food" in ps
    assert all(t in tm.valid_tags() for t in ps)


def test_tag_scoping_filters_when_store_is_tagged():
    store = _store()
    now = datetime(2026, 6, 1, 9, 0)
    occ = (now - timedelta(days=1)).isoformat()
    _add(store, "apples", "the kids like apples", occ)
    _add(store, "timesheet", "monthly timesheet due", occ)
    store.rebuild_projection()
    aid = store.conn.execute("SELECT belief_id FROM beliefs WHERE object='apples'").fetchone()[0]
    wid = store.conn.execute("SELECT belief_id FROM beliefs WHERE object='timesheet'").fetchone()[0]
    tm.set_tags(store.conn, aid, ["food", "meal"])
    tm.set_tags(store.conn, wid, ["work"])

    assert [b["object"] for b in beliefs_for_context(store, now, tags=["food", "meal"])] == ["apples"]
    assert [b["object"] for b in beliefs_for_context(store, now, tags=["work"])] == ["timesheet"]
    # no tag scope -> both (unchanged high-recall)
    assert set(b["object"] for b in beliefs_for_context(store, now)) == {"apples", "timesheet"}


def test_tag_scope_is_high_recall_on_untagged_store():
    # belief_tags empty -> the tag scope is inert (return all), never empty (fresh-install path).
    store = _store()
    now = datetime(2026, 6, 1, 9, 0)
    occ = (now - timedelta(days=1)).isoformat()
    _add(store, "apples", "the kids like apples", occ)
    store.rebuild_projection()
    assert [b["object"] for b in beliefs_for_context(store, now, tags=["food"])] == ["apples"]
