"""What the brain sees of past and ongoing work.

Pins: work that cites a concern (full id or the 8-character short form) or came from an email in
the matter's Gmail thread is linked exactly; similar work is found by meaning above a threshold,
best over every query text, never repeating linked work; the index embeds only what is new or
changed; only reminders that can still fire are shown, with readable intervals. Fake work rows and
a fake embedder; scratch index store. Invented data only.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.assistant.subconscious import work_links
from belief_engine.intake.store import sqlite_file

ROWS = []
VECTORS = {"bake sale": [1.0, 0.0, 0.0], "cookies for the bake sale": [0.9, 0.1, 0.0],
           "car service": [0.0, 1.0, 0.0], "tax return": [0.0, 0.0, 1.0]}


def row(wid, title, status="done", updated="2026-09-01T00:00:00+00:00", **constraints):
    return {"id": wid, "title": title, "status": status, "created_at": "2026-08-01T00:00:00+00:00",
            "updated_at": updated, "constraints": constraints}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    ROWS.clear()
    embedded = []
    monkeypatch.setattr(work_links, "_rows", lambda: list(ROWS))
    monkeypatch.setattr(work_links, "work_view", lambda wid, link, score=None: {"work_id": wid, "link": link,
                                                                                 "score": score})

    def embed(texts):
        embedded.extend(texts)
        return [VECTORS[t] for t in texts]
    monkeypatch.setattr(work_links, "_embed", embed)
    return embedded


@pytest.fixture
def connect(tmp_path):
    return sqlite_file(tmp_path / "index.db")


def test_work_citing_a_concern_or_from_the_thread_is_linked_exactly():
    ROWS.extend([
        row("w-full", "Book the vet", concern_refs=["concern:3f0c2b9a-1111-2222-3333-444455556666"]),
        row("w-short", "Remind about the vet", concern_refs=["concern:3f0c2b9a"]),
        row("w-thread", "Reply to the school", source_intake=[
            {"item_id": "dayflow_email:acct:u1", "source_type": "email", "thread_id": "t9"}]),
        row("w-other-account", "Other inbox", source_intake=[
            {"item_id": "dayflow_email:other:u2", "source_type": "email", "thread_id": "t9"}]),
        row("w-unrelated", "Unrelated", concern_refs=["concern:deadbeef"]),
    ])
    found = work_links.linked_work(["3f0c2b9a-1111-2222-3333-444455556666"],
                                   [{"account_id": "acct", "thread_id": "t9"}])
    assert {(w["work_id"], w["link"]) for w in found} == {
        ("w-full", "cites a concern of this matter"), ("w-short", "cites a concern of this matter"),
        ("w-thread", "created from an email in this thread")}


def test_similar_work_is_found_by_meaning_and_linked_work_is_not_repeated(connect):
    ROWS.extend([row("w-bake", "bake sale"), row("w-car", "car service"), row("w-tax", "tax return")])
    hits = work_links.similar_work(["cookies for the bake sale", "car service"], exclude=["w-car"],
                                   min_similarity=0.5, connect=connect)
    assert [h["work_id"] for h in hits] == ["w-bake"]
    assert hits[0]["score"] == pytest.approx(0.994, abs=0.01)


def test_the_index_embeds_only_what_changed(connect, fakes):
    ROWS.extend([row("w-bake", "bake sale"), row("w-car", "car service")])
    assert work_links.refresh_index(connect) == 2
    assert work_links.refresh_index(connect) == 0
    ROWS[1] = row("w-car", "tax return", updated="2026-09-02T00:00:00+00:00")
    assert work_links.refresh_index(connect) == 1
    assert fakes == ["bake sale", "car service", "tax return"]


def test_only_reminders_that_can_still_fire_are_shown():
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    rows = [("interval", 604800, "2025-11-03T18:00:00", None, '{"event_title": "Timesheets due"}'),
            ("interval", 86400, "2025-08-14T15:00:00", "2025-08-21T15:00:00", '{"event_title": "Ended course"}'),
            ("one_time_event", None, "2026-10-05T15:30:00", None, '{"event_title": "History quiz"}'),
            ("one_time_event", None, "2026-09-01T15:30:00", None, '{"event_title": "Past one-off"}'),
            ("interval", 31557600, "2026-10-16T16:00:00", None, '{"event_title": "Birthday (annual)"}')]
    live = work_links.live(rows, now)
    assert [(r["title"], r["kind"], r["repeats"]) for r in live] == [
        ("Birthday (annual)", "recurring", "every year"), ("History quiz", "one time", ""),
        ("Timesheets due", "recurring", "every week")]
