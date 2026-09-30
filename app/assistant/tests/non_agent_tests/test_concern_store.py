"""The concerns table (subconscious/concern_store.py): the register, stored as rows.

Pins: a register written is the register read (buckets, order, every field); tracked state is
queryable as columns; a concern in two buckets or without an id is refused; a concern dropped from
every bucket is removed; the one-time legacy import refuses to overwrite and renames the file; and a
plain read never touches the legacy file (the implicit import that renamed the real file from a
test on 2026-09-29). Scratch sqlite files; invented data only.
"""
from __future__ import annotations

import json

import pytest

from app.assistant.subconscious import concern_store
from app.assistant.tests.concern_store_helpers import ScratchRegister


def _c(cid, **kw):
    return {"concern_id": cid, "title": f"title {cid}", "severity": "low", "evidence": [{"ref": "x"}],
            "reinforcement_notes": "\n[t] note", **kw}


REGISTER = {"schema_version": 1, "last_updated_utc": "2026-03-01T00:00:00+00:00", "last_noticer_tick_utc": None,
            "active": [_c("b"), _c("a", anchor="calendar:e1")], "addressing": [_c("c")],
            "resolved": [_c("d", resolved_at_utc="2026-02-01T00:00:00+00:00")], "dormant": [_c("e", chronic=True)]}


def test_what_is_written_is_what_is_read(tmp_path):
    store = ScratchRegister(tmp_path).write(REGISTER)
    assert store.read() == REGISTER          # buckets, order within a bucket, every field, meta


def test_tracked_state_is_queryable(tmp_path):
    store = ScratchRegister(tmp_path).write(REGISTER)
    with store.connect(False) as c:
        rows = c.execute("SELECT concern_id, status, anchor, resolved_at_utc FROM concerns ORDER BY concern_id").fetchall()
    assert [tuple(r) for r in rows] == [("a", "active", "calendar:e1", None), ("b", "active", None, None),
                                        ("c", "addressing", None, None),
                                        ("d", "resolved", None, "2026-02-01T00:00:00+00:00"),
                                        ("e", "dormant", None, None)]


def test_a_concern_moved_between_buckets_keeps_one_row(tmp_path):
    store = ScratchRegister(tmp_path).write(REGISTER)
    reg = store.read()
    moved = reg["active"].pop(0)
    reg["resolved"].append(moved)
    store.write(reg)
    assert [c["concern_id"] for c in store.read()["resolved"]] == ["d", "b"]
    with store.connect(False) as c:
        assert c.execute("SELECT COUNT(*) FROM concerns").fetchone()[0] == 5


def test_a_concern_dropped_from_every_bucket_is_removed(tmp_path):
    store = ScratchRegister(tmp_path).write(REGISTER)
    reg = store.read()
    reg["dormant"] = []
    store.write(reg)
    assert "e" not in {c["concern_id"] for b in concern_store.BUCKETS for c in store.read()[b]}


@pytest.mark.parametrize("bad", [
    {"active": [_c("a")], "dormant": [_c("a")]},
    {"active": [{"title": "no id"}]},
])
def test_an_ambiguous_register_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        ScratchRegister(tmp_path).write(bad)


def test_the_legacy_import_moves_the_file_once(tmp_path):
    legacy = tmp_path / "resource_concerns_register.json"
    legacy.write_text(json.dumps(REGISTER), encoding="utf-8")
    store = ScratchRegister(tmp_path)
    assert concern_store.import_legacy_file(legacy, connect=store.connect) == 5
    assert store.read() == REGISTER
    assert not legacy.exists() and list(tmp_path.glob("resource_concerns_register.imported-*.json"))
    legacy.write_text(json.dumps(REGISTER), encoding="utf-8")
    with pytest.raises(RuntimeError, match="already holds"):
        concern_store.import_legacy_file(legacy, connect=store.connect)
    assert legacy.exists(), "a refused import leaves the file where it was"


def test_a_read_never_touches_the_legacy_file(tmp_path, monkeypatch):
    legacy = tmp_path / "resource_concerns_register.json"
    legacy.write_text(json.dumps(REGISTER), encoding="utf-8")
    monkeypatch.setattr(concern_store, "_legacy_file", lambda: legacy)
    store = ScratchRegister(tmp_path)
    assert store.read()["active"] == []
    assert legacy.exists()


def test_startup_imports_only_into_an_empty_table(tmp_path, monkeypatch):
    legacy = tmp_path / "resource_concerns_register.json"
    store = ScratchRegister(tmp_path)
    monkeypatch.setattr(concern_store, "_legacy_file", lambda: legacy)
    monkeypatch.setattr(concern_store, "_connect", store.connect)
    assert concern_store.import_legacy_file_if_pending() == 0            # no file: nothing to do
    legacy.write_text(json.dumps(REGISTER), encoding="utf-8")
    assert concern_store.import_legacy_file_if_pending() == 5
    legacy.write_text(json.dumps(REGISTER), encoding="utf-8")            # the old writer came back
    assert concern_store.import_legacy_file_if_pending() == 0
    assert legacy.exists(), "a populated table is never overwritten from the file"
