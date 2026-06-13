"""Boot-time chroma corruption self-heal (2026-06-12).

A corrupt derived collection access-violates on .count() and bricks boot;
the heal probes each collection in an isolated subprocess and drops the
ones that crash so the manager recreates them empty. The native crash
itself can't be unit-tested, but the parent's identify-and-drop logic is
pure: simulate the subprocess result and assert the right collection is
dropped.
"""
from __future__ import annotations

import types

from app.assistant.kg.chroma import chroma_health
from app.assistant.kg.chroma.chroma_health import (
    COLLECTION_NAMES,
    _OK_PREFIX,
    heal_corrupt_collections,
)


def _fake_proc(ok_names, returncode):
    stdout = "".join(f"{_OK_PREFIX}{n}\n" for n in ok_names)
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_healthy_index_drops_nothing(monkeypatch):
    monkeypatch.setattr(
        chroma_health.subprocess, "run",
        lambda *a, **k: _fake_proc(COLLECTION_NAMES, 0),
    )
    dropped = []
    monkeypatch.setattr(chroma_health, "_drop_collection", lambda n: dropped.append(n))

    assert heal_corrupt_collections() == []
    assert dropped == []


def test_corrupt_collection_identified_and_dropped(monkeypatch):
    # Pick the 4th collection as the corrupt one (matches the real incident:
    # node_context_embeddings). The child crashes after reporting OK for the
    # three before it.
    victim = COLLECTION_NAMES[3]
    before = COLLECTION_NAMES[:3]
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_proc(before, 3221225477)  # crash mid-walk
        return _fake_proc(COLLECTION_NAMES, 0)      # healthy after drop

    monkeypatch.setattr(chroma_health.subprocess, "run", fake_run)
    dropped = []
    monkeypatch.setattr(chroma_health, "_drop_collection", lambda n: dropped.append(n))

    healed = heal_corrupt_collections()
    assert healed == [victim]
    assert dropped == [victim]
    assert calls["n"] == 2  # one crash + one clean confirm


def test_two_corrupt_collections_healed_in_sequence(monkeypatch):
    v1, v2 = COLLECTION_NAMES[1], COLLECTION_NAMES[3]
    seq = [
        _fake_proc(COLLECTION_NAMES[:1], 3221225477),   # crash before v1
        _fake_proc(COLLECTION_NAMES[:3], 3221225477),   # next crash before v2
        _fake_proc(COLLECTION_NAMES, 0),                # clean
    ]
    monkeypatch.setattr(chroma_health.subprocess, "run", lambda *a, **k: seq.pop(0))
    dropped = []
    monkeypatch.setattr(chroma_health, "_drop_collection", lambda n: dropped.append(n))

    assert heal_corrupt_collections() == [v1, v2]
    assert dropped == [v1, v2]


def test_crash_with_no_identifiable_victim_drops_nothing(monkeypatch):
    # Child crashed but every collection reported OK — don't drop blind.
    monkeypatch.setattr(
        chroma_health.subprocess, "run",
        lambda *a, **k: _fake_proc(COLLECTION_NAMES, 3221225477),
    )
    dropped = []
    monkeypatch.setattr(chroma_health, "_drop_collection", lambda n: dropped.append(n))

    assert heal_corrupt_collections() == []
    assert dropped == []
