"""resource_files: the one read/list/write path for resources, across the code and user roots."""
import json
from pathlib import Path

import pytest

from app.assistant.utils import resource_files as rf
from app.assistant.utils import path_utils


@pytest.fixture
def roots(tmp_path, monkeypatch):
    code = tmp_path / "code" / "resources"
    user = tmp_path / "data" / "resources"
    code.mkdir(parents=True)
    user.mkdir(parents=True)
    monkeypatch.setattr(path_utils, "get_seed_resources_dir", lambda: code)
    monkeypatch.setattr(path_utils, "get_resources_dir", lambda: user)
    return code, user


def _put(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_template_is_read_until_the_owner_has_their_own(roots):
    code, user = roots
    _put(code, "instructions/house_rules.md.example", "generic rules")
    assert rf.read("instructions/house_rules.md") == "generic rules"

    rf.write("instructions/house_rules.md", "my rules")
    assert rf.read("instructions/house_rules.md") == "my rules"
    assert (code / "instructions/house_rules.md.example").read_text(encoding="utf-8") == "generic rules"
    assert not (code / "instructions/house_rules.md").exists()


def test_owner_file_wins_over_shipped_text(roots):
    code, user = roots
    _put(code, "a/x.md", "shipped")
    _put(user, "a/x.md", "mine")
    assert rf.read("a/x.md") == "mine"


def test_shipped_text_with_addon(roots):
    code, user = roots
    _put(code, "instructions/escalation.md", "general procedure\n")
    assert rf.read("instructions/escalation.md") == "general procedure\n"

    rf.write_addon("instructions/escalation.md", "\ncall Sam first")
    assert rf.read("instructions/escalation.md") == "general procedure\n\ncall Sam first"
    assert rf.read_base("instructions/escalation.md") == "general procedure\n"
    assert rf.read_addon("instructions/escalation.md") == "\ncall Sam first"
    assert (user / "instructions/escalation_personal.md").is_file()


def test_addon_applies_on_top_of_a_template_too(roots):
    code, _ = roots
    _put(code, "i/t.md.example", "template")
    rf.write_addon("i/t.md", "extra")
    assert rf.read("i/t.md") == "template\n\nextra"


def test_json_is_parsed_and_written_as_json(roots):
    code, user = roots
    _put(code, "user/profile.json.example", json.dumps({"name": ""}))
    assert rf.read("user/profile.json") == {"name": ""}
    rf.write("user/profile.json", {"name": "Sam"})
    assert json.loads((user / "user/profile.json").read_text(encoding="utf-8")) == {"name": "Sam"}
    assert rf.read("user/profile.json") == {"name": "Sam"}


def test_missing_resource_raises(roots):
    with pytest.raises(FileNotFoundError):
        rf.read("instructions/nope.md")
    assert rf.exists("instructions/nope.md") is False


def test_listing_merges_roots_strips_template_suffix_and_skips_addons(roots):
    code, user = roots
    _put(code, "instructions/a.md", "a")
    _put(code, "instructions/b.md.example", "b")
    _put(user, "instructions/b.md", "my b")
    _put(user, "instructions/a_personal.md", "add-on")
    _put(user, "outputs/c.json", "{}")
    assert rf.list_resources() == [
        Path("instructions/a.md"), Path("instructions/b.md"), Path("outputs/c.json"),
    ]
    assert rf.list_resources("instructions") == [Path("instructions/a.md"), Path("instructions/b.md")]


def test_writes_never_target_templates_or_addons_by_the_wrong_call(roots):
    with pytest.raises(ValueError):
        rf.write("i/t.md.example", "x")
    with pytest.raises(ValueError):
        rf.write("i/t_personal.md", "x")
    with pytest.raises(ValueError):
        rf.write("../outside.md", "x")


def test_mtime_follows_the_addon(roots):
    code, user = roots
    base = _put(code, "i/x.md", "base")
    before = rf.mtime("i/x.md")
    addon = rf.write_addon("i/x.md", "more")
    import os
    os.utime(addon, (before + 10, before + 10))
    assert rf.mtime("i/x.md") == pytest.approx(before + 10)
    assert rf.source_paths("i/x.md") == [base, addon]


def test_same_root_dev_checkout(tmp_path, monkeypatch):
    root = tmp_path / "resources"
    root.mkdir()
    monkeypatch.setattr(path_utils, "get_seed_resources_dir", lambda: root)
    monkeypatch.setattr(path_utils, "get_resources_dir", lambda: root)
    _put(root, "i/t.md.example", "template")
    assert rf.read("i/t.md") == "template"
    rf.write("i/t.md", "mine")
    assert rf.read("i/t.md") == "mine"
    assert (root / "i/t.md.example").read_text(encoding="utf-8") == "template"
    assert rf.list_resources() == [Path("i/t.md")]
