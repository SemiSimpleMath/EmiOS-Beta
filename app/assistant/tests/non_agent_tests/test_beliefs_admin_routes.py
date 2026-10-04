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


# ── the morning review (2026-10-03) ─────────────────────────────────────────────────────────────────────

def _review(client):
    data = client.get("/api/beliefs/review").get_json()
    return [b["belief_id"] for b in data["calendar"]], [b["belief_id"] for b in data["new"]]


def _state(client, bid):
    with client.store._connect(False) as c:
        return dict(c.execute("SELECT status, calendar, review, reviewed_day FROM belief_intake_beliefs WHERE id=?",
                              (bid,)).fetchone())


def test_the_review_shows_calendar_proposals_and_other_unreviewed_beliefs(client):
    client.store.apply("2026-09-03", _atom("The owner has tea at 4 PM.", "tea at four"), [0.9, 0.1], {"verdict": "new"})
    client.store.set_calendar("B3", "proposed", "every day at 4 PM: a habit at a time")
    with client.store._connect(True) as c:
        c.execute("UPDATE belief_intake_beliefs SET review='baseline' WHERE id='B1'")
    assert _review(client) == (["B3"], ["B2"])
    assert client.get("/api/beliefs/review").get_json()["calendar"][0]["calendar_note"].startswith("every day")


def test_review_decisions_write_the_calendar_and_the_review(client):
    client.store.set_calendar("B1", "proposed", "note")
    assert client.post("/api/beliefs/review/decide", json={"belief_id": "B1", "action": "add_to_calendar"}).status_code == 200
    assert _state(client, "B1") == {"status": "active", "calendar": "approved", "review": "approved",
                                    "reviewed_day": "2026-09-29"}
    client.post("/api/beliefs/review/decide", json={"belief_id": "B2", "action": "dismiss"})
    assert _state(client, "B2")["status"] == "retired" and _state(client, "B2")["review"] == "dismissed"
    assert _review(client) == ([], [])


def test_an_edited_proposal_stays_until_its_calendar_decision(client):
    client.store.set_calendar("B1", "proposed", "note")
    r = client.post("/api/beliefs/review/decide", json={"belief_id": "B1", "action": "edit",
                                                         "statement": "The owner likes tea at 4 PM."})
    assert r.get_json()["belief"]["statement"] == "The owner likes tea at 4 PM."
    assert _state(client, "B1")["review"] == "edited" and _review(client)[0] == ["B1"]
    owner = [e for e in client.get("/api/beliefs/item?belief_id=B1").get_json()["evidence"] if e["source_ref"] == "owner"]
    assert owner and owner[0]["text"] == "The owner likes tea at 4 PM."


def test_any_belief_can_be_put_on_the_calendar_from_the_drawer(client):
    client.post("/api/beliefs/update", json={"belief_id": "B2", "calendar": "approved"})
    assert _state(client, "B2")["calendar"] == "approved"
    assert client.post("/api/beliefs/update", json={"belief_id": "B2", "calendar": "maybe"}).status_code == 400


def test_an_unknown_review_action_is_refused(client):
    assert client.post("/api/beliefs/review/decide", json={"belief_id": "B1", "action": "keep"}).status_code == 400
    assert client.post("/api/beliefs/review/decide", json={"belief_id": "B1", "action": "edit", "statement": " "}).status_code == 400
