"""The personal overlay survives a reload, and an overlay-only edit is seen."""
import time
from pathlib import Path
import pytest
from app.resource_manager.resource_manager import ResourceManager


@pytest.fixture
def rm(tmp_path):
    m = ResourceManager.__new__(ResourceManager)
    import threading
    m._lock = threading.RLock()
    m._resource_values, m._resource_files, m._cached_mtimes = {}, {}, {}
    return m


def _pair(tmp_path, base="BASE TEXT", personal="PERSONAL TEXT"):
    b = tmp_path / "resource_prefs.md"
    b.write_text(base, encoding="utf-8")
    p = tmp_path / "resource_prefs_personal.md"
    p.write_text(personal, encoding="utf-8")
    return b, p


def test_read_applies_the_overlay(rm, tmp_path):
    b, _ = _pair(tmp_path)
    out = rm._read_with_overlay(b)
    assert "BASE TEXT" in out and "PERSONAL TEXT" in out


def test_a_reload_does_not_drop_the_overlay(rm, tmp_path):
    """Touching the base file used to replace the user's directives with the template."""
    b, _ = _pair(tmp_path)
    rm._resource_files["resource_prefs"] = b
    rm._set_cached_resource(resource_id="resource_prefs",
                            value=rm._read_with_overlay(b), source_path=b)
    time.sleep(0.01)
    b.write_text("BASE TEXT EDITED", encoding="utf-8")
    fresh = rm._get_cached_resource("resource_prefs")
    assert "BASE TEXT EDITED" in fresh
    assert "PERSONAL TEXT" in fresh, "the personal overlay was dropped by the reload"


def test_editing_only_the_overlay_is_noticed(rm, tmp_path):
    """The staleness check watches both files; an overlay-only edit used to be invisible."""
    b, p = _pair(tmp_path)
    rm._resource_files["resource_prefs"] = b
    rm._set_cached_resource(resource_id="resource_prefs",
                            value=rm._read_with_overlay(b), source_path=b)
    time.sleep(0.01)
    p.write_text("PERSONAL TEXT PLUS A NEW DIRECTIVE", encoding="utf-8")
    fresh = rm._get_cached_resource("resource_prefs")
    assert "A NEW DIRECTIVE" in fresh, "an overlay-only edit never reached a running process"


def test_no_overlay_file_is_fine(rm, tmp_path):
    b = tmp_path / "resource_solo.md"
    b.write_text("JUST THE BASE", encoding="utf-8")
    assert rm._read_with_overlay(b) == "JUST THE BASE"


def test_refresh_resource_keeps_the_overlay(rm, tmp_path):
    """The explicit refresh API is a re-read like any other, and dropped the overlay.

    2026-09-17: _read_with_overlay's own docstring says every path that re-reads from disk must
    go through it. Three did not — load_from_config, the JSON phase, and refresh_resource. The
    last is the dangerous one: refreshing the orchestrator prefs replaced the user's personal
    directives with the public template, silently, exactly the bug the overlay read was added
    to fix.
    """
    b, _ = _pair(tmp_path)
    rm._resource_files["resource_prefs"] = b
    rm._resource_values["resource_prefs"] = rm._read_with_overlay(b)
    rm.base_dir = tmp_path
    rm.refresh_resource("resource_prefs")
    assert "PERSONAL TEXT" in rm._resource_values["resource_prefs"], (
        "refresh_resource dropped the personal overlay")


def test_a_json_resource_is_unharmed_by_the_overlay_read(rm, tmp_path):
    """Routing every read through _read_with_overlay must not corrupt non-text resources:
    the append only fires when BOTH base and overlay are strings."""
    import json
    b = tmp_path / "resource_thing.json"
    b.write_text(json.dumps({"a": 1}), encoding="utf-8")
    (tmp_path / "resource_thing_personal.json").write_text(json.dumps({"b": 2}), encoding="utf-8")
    out = rm._read_with_overlay(b)
    assert out == {"a": 1}, f"JSON resource must come back unchanged, got {out!r}"
