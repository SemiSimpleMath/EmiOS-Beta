"""The owner's add-on survives a reload, and an add-on-only edit is seen.

The shipped text lives in the code root and the owner's `_personal` add-on in the user
root; the ResourceManager reads both only through resource_files.
"""
import json
import os
import time
from pathlib import Path

import pytest

from app.assistant.utils import resource_files
from app.assistant.utils import path_utils
from app.resource_manager.resource_manager import ResourceManager

REL = Path("instructions/resource_prefs.md")


@pytest.fixture
def roots(tmp_path, monkeypatch):
    code = tmp_path / "code"
    user = tmp_path / "user"
    code.mkdir()
    user.mkdir()
    monkeypatch.setattr(path_utils, "get_seed_resources_dir", lambda: code)
    monkeypatch.setattr(path_utils, "get_resources_dir", lambda: user)
    return code, user


@pytest.fixture
def rm():
    return ResourceManager()


def _pair(roots, base="BASE TEXT", personal="PERSONAL TEXT"):
    code, user = roots
    b = code / REL
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text(base, encoding="utf-8")
    p = user / resource_files.addon_rel(REL)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(personal, encoding="utf-8")
    return b, p


def _bump(path: Path) -> None:
    t = time.time() + 5
    os.utime(path, (t, t))


def _load(rm, rid="resource_prefs", rel=REL):
    rm._resource_files[rid] = rel
    rm._set_cached_resource(resource_id=rid, value=resource_files.read(rel), rel=rel)


def test_read_applies_the_addon(roots):
    _pair(roots)
    out = resource_files.read(REL)
    assert "BASE TEXT" in out and "PERSONAL TEXT" in out


def test_a_reload_does_not_drop_the_addon(rm, roots):
    """Touching the base file used to replace the user's directives with the template."""
    b, _ = _pair(roots)
    _load(rm)
    b.write_text("BASE TEXT EDITED", encoding="utf-8")
    _bump(b)
    fresh = rm._get_cached_resource("resource_prefs")
    assert "BASE TEXT EDITED" in fresh
    assert "PERSONAL TEXT" in fresh, "the add-on was dropped by the reload"


def test_editing_only_the_addon_is_noticed(rm, roots):
    """The staleness check watches both files; an add-on-only edit used to be invisible."""
    _, p = _pair(roots)
    _load(rm)
    p.write_text("PERSONAL TEXT PLUS A NEW DIRECTIVE", encoding="utf-8")
    _bump(p)
    fresh = rm._get_cached_resource("resource_prefs")
    assert "A NEW DIRECTIVE" in fresh, "an add-on-only edit never reached a running process"


def test_no_addon_file_is_fine(roots):
    code, _ = roots
    (code / "resource_solo.md").write_text("JUST THE BASE", encoding="utf-8")
    assert resource_files.read("resource_solo.md") == "JUST THE BASE"


def test_refresh_resource_keeps_the_addon(rm, roots):
    """The explicit refresh API is a re-read like any other, and once dropped the add-on."""
    _pair(roots)
    rm._resource_files["resource_prefs"] = REL
    rm._resource_values["resource_prefs"] = "stale"
    rm.refresh_resource("resource_prefs")
    assert "PERSONAL TEXT" in rm._resource_values["resource_prefs"], (
        "refresh_resource dropped the add-on")


def test_a_json_resource_is_unharmed_by_the_addon_read(roots):
    """The add-on join only applies to text; a JSON resource comes back unchanged."""
    code, user = roots
    (code / "resource_thing.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    (user / "resource_thing_personal.json").write_text(json.dumps({"b": 2}), encoding="utf-8")
    assert resource_files.read("resource_thing.json") == {"a": 1}


def test_load_all_reads_owner_files_templates_and_addons_from_both_roots(rm, roots, monkeypatch):
    """The data-folder bug of 2026-11-06: nothing in the user root was loaded at boot."""
    code, user = roots
    monkeypatch.setattr("app.resource_manager.resource_manager.DI", type("D", (), {"global_blackboard": None})())
    (code / "instructions").mkdir(exist_ok=True)
    (code / "instructions/resource_rules.md.example").write_text("generic rules", encoding="utf-8")
    (code / "instructions/resource_shipped.md").write_text("shipped", encoding="utf-8")
    (user / "instructions").mkdir(exist_ok=True)
    (user / "instructions/resource_shipped_personal.md").write_text("my addition", encoding="utf-8")
    (user / "user").mkdir()
    (user / "user/resource_user_data.json").write_text(json.dumps({"name": "Sam"}), encoding="utf-8")
    (code / "user").mkdir()
    (code / "user/resource_user_data.json.example").write_text(json.dumps({"name": ""}), encoding="utf-8")

    rm.load_all()

    assert rm._resource_values["resource_rules"] == "generic rules"
    assert rm._resource_values["resource_shipped"] == "shipped\n\nmy addition"
    assert rm._resource_values["resource_user_data"] == {"name": "Sam"}
    assert "resource_shipped_personal" not in rm._resource_values


def test_update_resource_writes_the_user_root_never_the_code_root(rm, roots, monkeypatch):
    code, user = roots
    monkeypatch.setattr("app.resource_manager.resource_manager.DI", type("D", (), {"global_blackboard": None})())
    (code / "rules.md.example").write_text("generic", encoding="utf-8")
    _load(rm, "rules", Path("rules.md"))
    rm.update_resource("rules", "learned")
    assert (user / "rules.md").read_text(encoding="utf-8") == "learned"
    assert (code / "rules.md.example").read_text(encoding="utf-8") == "generic"
    assert not (code / "rules.md").exists()
