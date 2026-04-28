"""
Tests for ResourceManager's auto-reload-on-mtime-advance behavior.

Regression context: resources were cached at Flask startup and never
invalidated. Pipeline stages that wrote raw files (e.g. dayflow_routine_stage
writing resource_dayflow_routine.md at 6AM each morning) updated disk but
never touched the cache — agents kept seeing the boot-time version for the
entire process lifetime. These tests lock in the invariant that
``get_resource`` detects on-disk changes and serves fresh content.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401
from app.resource_manager.resource_manager import ResourceManager


def _scope(resource_ids: list[str]) -> dict:
    return {"resources": {"allowed_global_resources": resource_ids}}


def _touch_newer(path: Path, seconds_forward: float = 2.0) -> None:
    """Bump the file's mtime forward. Python's write_text can produce an
    mtime equal to (or within filesystem resolution of) the previous value
    on fast sequential writes — this helper guarantees a strictly greater
    mtime so the staleness check fires deterministically.
    """
    now = time.time()
    os.utime(path, (now + seconds_forward, now + seconds_forward))


def test_auto_reload_when_file_mtime_advances(tmp_path):
    """After an external write to the backing file, next get_resource sees fresh content."""
    rm = ResourceManager(base_dir=tmp_path)
    res_file = tmp_path / "resource_test_doc.json"
    res_file.write_text(json.dumps({"value": "first"}), encoding="utf-8")

    rm._resource_files["resource_test_doc"] = res_file
    # Seed the cache as if loaded at startup.
    rm._set_cached_resource(
        resource_id="resource_test_doc",
        value={"value": "first"},
        source_path=res_file,
    )

    # Simulate an external pipeline write of NEW content + bump mtime forward.
    res_file.write_text(json.dumps({"value": "second"}), encoding="utf-8")
    _touch_newer(res_file)

    result = rm.get_resource(
        scope_context=_scope(["resource_test_doc"]),
        resource_id="resource_test_doc",
    )
    assert result == {"value": "second"}, (
        f"Expected auto-reload to pick up new file content, got {result!r}"
    )


def test_stable_cache_when_file_unchanged(tmp_path):
    """If the file hasn't changed, return the cached value without re-reading."""
    rm = ResourceManager(base_dir=tmp_path)
    res_file = tmp_path / "resource_stable.json"
    res_file.write_text(json.dumps({"value": "original"}), encoding="utf-8")

    rm._resource_files["resource_stable"] = res_file
    rm._set_cached_resource(
        resource_id="resource_stable",
        value={"value": "original"},
        source_path=res_file,
    )

    # Spy on _read_file — should NOT be called when mtime hasn't advanced.
    read_calls: list[Path] = []
    original_read_file = rm._read_file
    def _spy_read(path: Path):
        read_calls.append(path)
        return original_read_file(path)
    rm._read_file = _spy_read

    for _ in range(3):
        out = rm.get_resource(
            scope_context=_scope(["resource_stable"]),
            resource_id="resource_stable",
        )
        assert out == {"value": "original"}

    assert read_calls == [], (
        f"get_resource should not re-read unchanged files; was read {len(read_calls)} times"
    )


def test_file_vanished_falls_back_to_cache(tmp_path):
    """If the backing file is deleted, return the last-known cached value instead of crashing."""
    rm = ResourceManager(base_dir=tmp_path)
    res_file = tmp_path / "resource_transient.json"
    res_file.write_text(json.dumps({"value": "cached"}), encoding="utf-8")

    rm._resource_files["resource_transient"] = res_file
    rm._set_cached_resource(
        resource_id="resource_transient",
        value={"value": "cached"},
        source_path=res_file,
    )

    res_file.unlink()

    out = rm.get_resource(
        scope_context=_scope(["resource_transient"]),
        resource_id="resource_transient",
    )
    assert out == {"value": "cached"}


def test_corrupt_file_after_reload_falls_back_to_cache(tmp_path):
    """If the file changed but is now unparseable, keep serving the previous cached value
    rather than failing the call. Operator sees a WARNING log; agents stay functional.
    """
    rm = ResourceManager(base_dir=tmp_path)
    res_file = tmp_path / "resource_fragile.json"
    res_file.write_text(json.dumps({"value": "good"}), encoding="utf-8")

    rm._resource_files["resource_fragile"] = res_file
    rm._set_cached_resource(
        resource_id="resource_fragile",
        value={"value": "good"},
        source_path=res_file,
    )

    # Overwrite with invalid JSON, then bump mtime forward.
    res_file.write_text("this is not valid json {{{", encoding="utf-8")
    _touch_newer(res_file)

    out = rm.get_resource(
        scope_context=_scope(["resource_fragile"]),
        resource_id="resource_fragile",
    )
    assert out == {"value": "good"}, (
        "Should fall back to previous cached value when reload fails"
    )


def test_update_resource_tracks_mtime_for_future_external_edits(tmp_path):
    """After update_resource writes to disk, the cached_mtime is re-anchored so
    a later external write is still detected.
    """
    rm = ResourceManager(base_dir=tmp_path)
    res_file = tmp_path / "resource_update_me.json"
    res_file.write_text(json.dumps({"value": "v0"}), encoding="utf-8")

    rm._resource_files["resource_update_me"] = res_file
    rm._set_cached_resource(
        resource_id="resource_update_me",
        value={"value": "v0"},
        source_path=res_file,
    )

    # Agent-driven update via update_resource (also writes the file).
    rm.update_resource("resource_update_me", {"value": "v1"}, persist=True)
    assert rm._cached_mtimes.get("resource_update_me") is not None, (
        "update_resource should re-anchor cached_mtime after disk write"
    )

    # Now simulate a third-party pipeline writing the file again.
    res_file.write_text(json.dumps({"value": "v2"}), encoding="utf-8")
    _touch_newer(res_file)

    out = rm.get_resource(
        scope_context=_scope(["resource_update_me"]),
        resource_id="resource_update_me",
    )
    assert out == {"value": "v2"}


def test_text_file_resource_also_auto_reloads(tmp_path):
    """The bug that started all this: a .md file rewritten by a pipeline stage.
    Confirm the same auto-reload works for plain text just like JSON.
    """
    rm = ResourceManager(base_dir=tmp_path)
    res_file = tmp_path / "resource_routine.md"
    res_file.write_text("# Routine\n- wake early\n", encoding="utf-8")

    rm._resource_files["resource_routine"] = res_file
    rm._set_cached_resource(
        resource_id="resource_routine",
        value="# Routine\n- wake early\n",
        source_path=res_file,
    )

    res_file.write_text("# Routine\n- wake early\n- drink water\n", encoding="utf-8")
    _touch_newer(res_file)

    out = rm.get_resource(
        scope_context=_scope(["resource_routine"]),
        resource_id="resource_routine",
    )
    assert "drink water" in out
