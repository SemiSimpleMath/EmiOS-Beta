"""A contradiction waits for the owner (2026-10-03). Pins: an atom that contradicts held beliefs becomes its own
pending belief naming every belief it contradicts (the one dedup named and the ones the fan-out finds), with a
drafted rewrite for each that reconciles them, and the held beliefs stay as they were; a pending belief is invisible to dedup and the calendar tagger; the owner applies the
drafted rewrites (the held beliefs take the new wording and the new belief's evidence as support), keeps the new
one (the old ones retire), the old one (the new one retires) or both; the review lists each held contradiction
with both sides' evidence. Scratch store; fake dedup; invented data only.
"""
from __future__ import annotations

import pytest
from flask import Flask

import app.routes.beliefs as R
from belief_engine.intake import agents, run
from belief_engine.intake.store import IntakeStore, sqlite_file


def _atom(statement, text="said so"):
    return {"statement": statement, "kind": "routine_pattern", "scope": "chronic",
            "sources": [{"time": "2026-01-02 10:00", "kind": "said", "relation": "support", "text": text,
                         "source_ref": "message:9"}]}


@pytest.fixture
def store(tmp_path):
    s = IntakeStore(sqlite_file(tmp_path / "s.db"), "belief_intake_")
    for st in ("The owner sets the AC to 70 at night.", "The owner keeps the AC at 70 all night.",
               "The owner likes tea."):
        s.apply("2026-01-01", _atom(st), [1.0, 0.0], {"verdict": "new"})
    return s


def test_a_contradiction_is_held_with_every_belief_it_contradicts(store, monkeypatch):
    verdicts = iter([{"verdict": "contradicts", "target": "B1", "sources": [1], "reasoning": "72 now"},
                     {"verdict": "contradicts", "target": "B2", "sources": [1], "reasoning": "72 now"},
                     {"verdict": "new", "target": None, "sources": [], "reasoning": "nothing else"}])
    monkeypatch.setattr(agents, "dedup", lambda *a, **k: next(verdicts))
    drafted = {"B1": {"outcome": "revised", "statement": "The owner sets the AC to 72 at night.",
                      "kind": "routine_pattern", "reasoning": "said so"},
               "B2": {"outcome": "unchanged", "statement": "held", "kind": "routine_pattern", "reasoning": "one night"}}
    monkeypatch.setattr(agents, "draft_revision", lambda belief, *a, **k: drafted[belief["id"]])
    bid = run.judge_atom("2026-01-02", _atom("The owner sets the AC to 72 at night.", "72 is better"), [1.0, 0.0],
                         store, scope_ctx=None, log=lambda line: None)
    assert bid == "B4"
    with store._connect(False) as c:
        row = c.execute("SELECT status, contradicts, drafts FROM belief_intake_beliefs WHERE id='B4'").fetchone()
        held = [r[0] for r in c.execute("SELECT statement FROM belief_intake_beliefs WHERE id IN ('B1','B2') ORDER BY id")]
    assert (row["status"], row["contradicts"]) == ("pending", '["B1", "B2"]')
    assert list(__import__("json").loads(row["drafts"])) == ["B1"], "a target no rewrite reconciles has no draft"
    assert held == ["The owner sets the AC to 70 at night.", "The owner keeps the AC at 70 all night."], "nothing revised"
    assert "B4" not in [b["id"] for b in store.beliefs()] and "B4" not in [b["id"] for b in store.calendar_undecided()]


@pytest.mark.parametrize("keep,new_status,old_status,review", [
    ("new", "active", "retired", "approved"), ("old", "retired", "active", "dismissed"),
    ("both", "active", "active", "approved")])
def test_the_owners_decision_on_a_held_contradiction(store, keep, new_status, old_status, review):
    bid = store.hold("2026-01-02", _atom("The owner sets the AC to 72 at night."), [1.0, 0.0], ["B1"], "72 now")
    store.resolve_contradiction(bid, keep, "2026-01-03")
    assert store.get(bid)["status"] == new_status and store.get("B1")["status"] == old_status
    with store._connect(False) as c:
        assert c.execute("SELECT review FROM belief_intake_beliefs WHERE id=?", (bid,)).fetchone()[0] == review
    with pytest.raises(ValueError, match="not a held contradiction"):
        store.resolve_contradiction(bid, keep, "2026-01-03")


@pytest.fixture
def client(store, monkeypatch, tmp_path):
    connect = store._connect
    with connect(True) as c:
        c.execute("CREATE TABLE belief_tags (belief_id TEXT NOT NULL, tag TEXT NOT NULL, assigned_at TEXT, "
                  "method TEXT, PRIMARY KEY (belief_id, tag))")
    monkeypatch.setattr(R, "_store", lambda: store)
    monkeypatch.setattr(R, "_read", lambda: connect(False))
    monkeypatch.setattr(R, "_write", lambda: connect(True))
    monkeypatch.setattr(R, "_today", lambda: "2026-01-03")
    import sys
    import belief_engine.export.export_beliefs  # noqa: F401
    monkeypatch.setattr(sys.modules["belief_engine.export.export_beliefs"], "export_beliefs", lambda: None)
    monkeypatch.setattr("app.assistant.embeddings.embedder.embed_texts", lambda texts: [[0.5, 0.5] for _ in texts])
    app = Flask(__name__)
    app.register_blueprint(R.beliefs_admin_bp)
    return app.test_client()


def test_the_review_lists_each_held_contradiction_with_both_sides(store, client):
    bid = store.hold("2026-01-02", _atom("The owner sets the AC to 72 at night.", "72 is better"), [1.0, 0.0],
                     ["B1"], "72 now")
    [held] = client.get("/api/beliefs/review").get_json()["contradictions"]
    assert held["belief_id"] == bid and held["evidence"][0]["text"] == "72 is better"
    assert held["contradicts"][0]["belief_id"] == "B1" and held["contradicts"][0]["evidence"][0]["text"] == "said so"
    r = client.post("/api/beliefs/review/decide", json={"belief_id": bid, "action": "edit",
                                                         "statement": "The owner sets the AC to 72 at bedtime."})
    assert r.status_code == 200 and store.get(bid)["status"] == "pending", "an edited contradiction stays held"
    assert client.post("/api/beliefs/review/decide", json={"belief_id": bid, "action": "keep_new"}).status_code == 200
    assert client.get("/api/beliefs/review").get_json()["contradictions"] == []
    assert store.get("B1")["status"] == "retired"
    assert client.post("/api/beliefs/review/decide", json={"belief_id": "B3", "action": "keep_new"}).status_code == 400


def test_applying_the_rewrite_rewords_the_held_belief_and_folds_the_new_one_in(store):
    bid = store.hold("2026-01-02", _atom("The owner sets the AC to 72 at night.", "72 is better"), [1.0, 0.0], ["B1"],
                     "72 now", {"B1": {"statement": "The owner sets the AC to 72 at night.", "kind": "routine_pattern",
                                       "reasoning": "r"}})
    store.resolve_contradiction(bid, "rewrite", "2026-01-03", {"B1": ("The owner sets the AC to 72 at night.", [1.0, 0.0])})
    b1 = next(b for b in store.beliefs() if b["id"] == "B1")
    assert b1["statement"] == "The owner sets the AC to 72 at night."
    assert [s["text"] for s in b1["sources"]] == ["said so", "72 is better"], "the new statement supports it now"
    assert store.get(bid)["status"] == "retired"
    with pytest.raises(ValueError, match="does not"):
        other = store.hold("2026-01-04", _atom("x"), [1.0, 0.0], ["B2"], "r", {})
        store.resolve_contradiction(other, "rewrite", "2026-01-04", {"B3": ("y", [1.0, 0.0])})


def test_the_review_shows_the_draft_and_applies_the_owners_edit_of_it(store, client):
    bid = store.hold("2026-01-02", _atom("The owner sets the AC to 72 at night."), [1.0, 0.0], ["B1"], "72 now",
                     {"B1": {"statement": "The owner sets the AC to 72 at night.", "kind": "routine_pattern",
                             "reasoning": "r"}})
    [held] = client.get("/api/beliefs/review").get_json()["contradictions"]
    assert held["contradicts"][0]["draft"] == "The owner sets the AC to 72 at night."
    r = client.post("/api/beliefs/review/decide", json={"belief_id": bid, "action": "apply_rewrite",
                                                         "rewrites": {"B1": "The owner sets the AC to 72 at bedtime."}})
    assert r.status_code == 200
    assert store.get("B1")["statement"] == "The owner sets the AC to 72 at bedtime."
    assert client.post("/api/beliefs/review/decide", json={"belief_id": "B2", "action": "apply_rewrite",
                                                            "rewrites": {}}).status_code == 400
