"""/api/personalize/directives over the six role pieces of the owner's directives (2026-10-08).

A scratch repo root stands in for the real one, so no resource file is read or written. Pins
the allowlist: the list names every piece and who reads it; a GET of a piece returns its shared
template, its overlay and the combined text the ResourceManager builds; a POST writes only that
piece's overlay (creating it when absent) and never the template; an unknown piece is a 400."""
from __future__ import annotations

import pytest
from flask import Flask

import app.routes.personalize as P

_IDS = [
    "resource_assistant_capabilities",
    "resource_owner_standing_directives",
    "resource_work_shaping",
    "resource_worth_doing",
    "resource_task_approach",
    "resource_owner_escalation",
]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    instr = tmp_path / "resources" / "instructions"
    instr.mkdir(parents=True)
    (instr / "resource_work_shaping.md").write_text("# How to shape work\n- shared line\n", encoding="utf-8")
    (instr / "resource_work_shaping_personal.md").write_text("- my line\n", encoding="utf-8")
    (instr / "resource_task_approach.md").write_text("# How to approach a task\n", encoding="utf-8")
    monkeypatch.setattr(P, "get_repo_root", lambda: str(tmp_path))
    return instr


@pytest.fixture
def client(repo):
    app = Flask(__name__)
    app.register_blueprint(P.personalize_bp)
    return app.test_client()


def _files(instr):
    return {p.name: p.read_text(encoding="utf-8") for p in instr.iterdir()}


def test_list_names_the_six_pieces_and_who_reads_them(client):
    data = client.get("/api/personalize/directives").get_json()
    assert data["success"] is True
    assert [p["id"] for p in data["pieces"]] == _IDS
    by_id = {p["id"]: p for p in data["pieces"]}
    assert by_id["resource_work_shaping"]["read_by"] == ["the work architect"]
    assert by_id["resource_owner_escalation"]["read_by"] == []
    assert by_id["resource_task_approach"]["personal_filename"] == "resource_task_approach_personal.md"


def test_get_piece_returns_template_overlay_and_combined(client):
    data = client.get("/api/personalize/directives?piece=resource_work_shaping").get_json()
    assert data["piece"]["title"] == "How to shape work"
    assert data["base"] == {"filename": "resource_work_shaping.md",
                            "content": "# How to shape work\n- shared line\n", "exists": True}
    assert data["personal"]["exists"] is True and data["personal"]["content"] == "- my line\n"
    assert data["combined"] == "# How to shape work\n- shared line\n\n- my line\n"


def test_get_piece_without_overlay_shows_it_empty(client):
    data = client.get("/api/personalize/directives?piece=resource_task_approach").get_json()
    assert data["personal"] == {"filename": "resource_task_approach_personal.md", "content": "", "exists": False}
    assert data["combined"] == "# How to approach a task"


def test_post_writes_only_the_pieces_overlay(client, repo):
    before = _files(repo)
    r = client.post("/api/personalize/directives",
                    json={"piece": "resource_task_approach", "content": "- ask me first\n"})
    data = r.get_json()
    assert r.status_code == 200 and data["success"] is True
    assert data["combined"] == "# How to approach a task\n\n- ask me first\n"
    after = _files(repo)
    assert set(after) - set(before) == {"resource_task_approach_personal.md"}
    assert after["resource_task_approach_personal.md"] == "- ask me first\n"
    assert {k: v for k, v in after.items() if k in before} == before


def test_unknown_piece_is_a_400_and_writes_nothing(client, repo):
    before = _files(repo)
    assert client.get("/api/personalize/directives?piece=resource_orchestrator_user_prefs").status_code == 400
    assert client.get("/api/personalize/directives?piece=").status_code == 400
    for body in ({"piece": "../../app/evil", "content": "x"},
                 {"piece": "resource_orchestrator_user_prefs", "content": "x"},
                 {"content": "x"}):
        r = client.post("/api/personalize/directives", json=body)
        assert r.status_code == 400 and r.get_json()["success"] is False
    assert _files(repo) == before


def test_old_dayflow_skill_editor_redirects_to_the_piece_editor():
    import app.routes.preferences as PR
    app = Flask(__name__)
    app.register_blueprint(PR.preferences_bp)
    app.register_blueprint(P.personalize_bp)
    r = app.test_client().get("/skills/dayflow")
    assert r.status_code == 302 and r.headers["Location"].endswith("/personalize/directives")
    assert "dayflow" not in PR._EDITABLE_SKILL_PATHS
    assert app.test_client().get("/api/skills/dayflow").status_code == 404


def test_resource_editor_lists_the_six_shared_templates_not_the_old_file():
    import app.routes.preferences as PR
    specs = PR._IMPORTANT_RESOURCE_SPECS
    assert "resource_orchestrator_user_prefs" not in specs
    for rid in _IDS:
        assert specs[rid]["path"] == ("instructions", f"{rid}.md") and specs[rid]["kind"] == "markdown"
