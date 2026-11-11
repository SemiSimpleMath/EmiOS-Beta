"""text_file_access.resolve_tool_path: the one resolver the read/write/append text-file tools share."""
from pathlib import Path

import pytest

from app.assistant.lib.tools import text_file_access as tfa
from app.assistant.utils import path_utils


@pytest.fixture
def roots(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    user = tmp_path / "data" / "resources"
    (repo / "resources").mkdir(parents=True)
    user.mkdir(parents=True)
    monkeypatch.setattr(tfa, "get_repo_root", lambda: repo)
    monkeypatch.setattr(path_utils, "get_resources_dir", lambda: user)
    return repo, user


def test_resources_paths_go_to_the_user_root(roots):
    repo, user = roots
    path, logical = tfa.resolve_tool_path("resources/dayflow_pipeline_outputs/x.md")
    assert path == (user / "dayflow_pipeline_outputs/x.md").resolve()
    assert logical == "resources/dayflow_pipeline_outputs/x.md"


def test_other_paths_stay_under_the_repo(roots):
    repo, _ = roots
    path, logical = tfa.resolve_tool_path("outputs/a.md")
    assert path == (repo / "outputs/a.md").resolve()
    assert logical == "outputs/a.md"


def test_absolute_paths_in_either_root_are_named_logically(roots):
    repo, user = roots
    assert tfa.resolve_tool_path(str(user / "a/b.md"))[1] == "resources/a/b.md"
    assert tfa.resolve_tool_path(str(repo / "outputs/c.md"))[1] == "outputs/c.md"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "resources/../../x", "resources/../../../x"])
def test_escapes_are_refused(roots, bad):
    assert tfa.resolve_tool_path(bad) is None


def test_absolute_outside_both_roots_is_refused(roots, tmp_path):
    assert tfa.resolve_tool_path(str(tmp_path / "elsewhere.md")) is None


def test_allowlist_matches_the_logical_name(roots):
    _, logical = tfa.resolve_tool_path("resources/dayflow_pipeline_outputs/x.md")
    assert tfa.is_allowed(logical, ["resources/dayflow_pipeline_outputs/*"])
    assert not tfa.is_allowed(logical, ["outputs/*"])
    assert tfa.is_allowed(logical, None)
