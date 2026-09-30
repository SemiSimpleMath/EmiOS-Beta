"""/beliefs admin API over the live catalog (the intake store). Real IntakeStore in a scratch
file; the export, embedder and clock are stubbed. Pins what an owner correction writes: a
revision plus the owner's words as evidence, retire/restore in the history, manual tags."""
from __future__ import annotations

import pytest
from flask import Flask

import app.routes.beliefs as R
from belief_engine.intake.store import IntakeStore, sqlite_file


def _atom(statement, text):
    return {"statement": statement, "kind": "stable_preference", "scope": "chronic",
            "sources": [{"time": "2026-09-01 10:00", "kind": "said", "relation": "support",
                         "text": text, "source_ref": "message:1"}]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    connect = sqlite_file(tmp_path / "catalog.db")
    store = IntakeStore(connect, "belief_intake_")
    with connect(True) as c:
        c.execute("CREATE TABLE belief_tags (belief_id TEXT NOT NULL, tag TEXT NOT NULL, assigned_at TEXT, "
                  "method TEXT, PRIMARY KEY (belief_id, tag))")
    store.apply("2026-09-01", _atom("The owner likes tea.", "i like tea"), [1.0, 0.0], {"verdict": "new"})
    store.apply("2026-09-02", _atom("The owner likes green tea.", "green tea please"), [1.0, 0.1],
                {"verdict": "refines", "target": "B1"})
    monkeypatch.setattr(R, "_store", lambda: store)
    monkeypatch.setattr(R, "_read", lambda: connect(False))
    monkeypatch.setattr(R, "_write", lambda: connect(True))
    monkeypatch.setattr(R, "_today", lambda: "2026-09-29")
    import sys
    import belief_engine.export.export_beliefs  # noqa: F401 — the package re-exports the function under the module's name
    monkeypatch.setattr(sys.modules["belief_engine.export.export_beliefs"], "export_beliefs", lambda: None)
    monkeypatch.setattr("app.assistant.embeddings.embedder.embed_texts", lambda texts: [[0.5, 0.5] for _ in texts])
    app = Flask(__name__)
    app.register_blueprint(R.beliefs_admin_bp)
    c = app.test_client()
    c.store = store
    return c


def test_list_and_item_read_the_catalog(client):
    rows = client.get("/api/beliefs/list").get_json()["beliefs"]
    assert {r["belief_id"] for r in rows} == {"B1", "B2"}
    item = client.get("/api/beliefs/item?belief_id=B1").get_json()
    assert item["belief"]["support"] == 1 and item["evidence"][0]["text"] == "i like tea"
    assert [c["belief_id"] for c in item["children"]] == ["B2"]
    assert client.get("/api/beliefs/item?belief_id=B2").get_json()["parent"]["belief_id"] == "B1"


def test_statement_correction_is_a_revision_plus_the_owners_words(client):
    r = client.post("/api/beliefs/update", json={"belief_id": "B1", "statement": "The owner prefers coffee."})
    assert r.get_json()["belief"]["statement"] == "The owner prefers coffee."
    item = client.get("/api/beliefs/item?belief_id=B1").get_json()
    assert item["revisions"][0]["old_statement"] == "The owner likes tea."
    owner = [e for e in item["evidence"] if e["source_ref"] == "owner"]
    assert owner and owner[0]["kind"] == "said" and owner[0]["text"] == "The owner prefers coffee."


def test_retire_and_restore_keep_history(client):
    client.post("/api/beliefs/update", json={"belief_id": "B2", "retired": True})
    assert [r["belief_id"] for r in client.get("/api/beliefs/list").get_json()["beliefs"]] == ["B1"]
    assert client.get("/api/beliefs/list?status=retired").get_json()["beliefs"][0]["belief_id"] == "B2"
    client.post("/api/beliefs/update", json={"belief_id": "B2", "retired": False})
    reasons = [x["reasoning"] for x in client.get("/api/beliefs/item?belief_id=B2").get_json()["revisions"]]
    assert reasons[0].startswith("restored:") and reasons[1].startswith("retired:")


def test_tags_are_manual_and_vocab_checked(client):
    client.post("/api/beliefs/update", json={"belief_id": "B1", "tags": ["beverage", "not_a_tag"]})
    with client.store._connect(False) as c:
        rows = c.execute("SELECT tag, method FROM belief_tags WHERE belief_id='B1'").fetchall()
    assert [tuple(r) for r in rows] == [("beverage", "manual")]
    assert client.get("/api/beliefs/list?tag=beverage").get_json()["count"] == 1


def test_trends_count_support_and_list_revisions(client):
    client.post("/api/beliefs/update", json={"belief_id": "B1", "statement": "The owner prefers coffee."})
    t = client.get("/api/beliefs/trends?days=400&min_net=1").get_json()
    assert {r["belief_id"] for r in t["trending_up"]} == {"B1", "B2"}
    assert t["recently_changed"][0]["belief_id"] == "B1"
