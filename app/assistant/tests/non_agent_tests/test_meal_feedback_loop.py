"""Guard: the meal-feedback loop asks about past meals and ingests replies.

PRODUCE: a recent past meal with no feedback-asked marker enqueues a
meal_feedback question, records the question->meal mapping, and stamps the pod.
INGEST: once the bridge has asked it, the user's chat reply is minted as a
feedback.comment targeting that meal pod (-> feedback_extractor -> beliefs).

Hermetic: all of PodStore / enqueue / mint / dismiss / status / reply-fetch are
monkeypatched, so this never touches the live DB. Part of the pre-push guard.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.assistant.subconscious.meal_feedback_runner as mfr


class _Pod:
    def __init__(self, pod_id, metadata):
        self.pod_id = pod_id
        self.metadata = metadata


class _FakeStore:
    def __init__(self, pods):
        self._pods = pods
        self.put_calls = []

    def query(self, *, kind, since_utc=None, limit=200):
        return self._pods if kind == "intention.meal" else []

    def put(self, pod):
        self.put_calls.append(pod)


def test_produce_enqueues_for_past_meal(monkeypatch):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    pod = _Pod("datapod:intention.meal:t1",
               {"dish": "Sheet-pan tacos", "date": yesterday, "meal_window": "dinner"})
    store = _FakeStore([pod])
    monkeypatch.setattr("app.assistant.pod_store.pod_store.PodStore", lambda: store)

    captured = {}

    def fake_enqueue(*, question_text, topical_tag, priority, created_by, expires_after_hours):
        captured["text"] = question_text
        captured["tag"] = topical_tag
        return "qid-1"

    monkeypatch.setattr("app.assistant.pending_questions.store.enqueue_question", fake_enqueue)

    state = {"active": {}}
    summary = {}
    mfr._produce(state, now, summary, dry_run=False)

    assert summary["asked"] == 1, summary
    assert "qid-1" in state["active"], state
    assert state["active"]["qid-1"]["meal_pod_id"] == "datapod:intention.meal:t1"
    assert pod.metadata.get("feedback_asked_at_utc")          # durable idempotency stamp
    assert "Sheet-pan tacos" in captured["text"]
    assert captured["tag"] == "meal_feedback"


def test_produce_skips_already_asked(monkeypatch):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    pod = _Pod("datapod:intention.meal:t2",
               {"dish": "Salmon", "date": yesterday, "meal_window": "dinner",
                "feedback_asked_at_utc": "2026-06-08T09:00:00+00:00"})
    monkeypatch.setattr("app.assistant.pod_store.pod_store.PodStore", lambda: _FakeStore([pod]))
    monkeypatch.setattr("app.assistant.pending_questions.store.enqueue_question",
                        lambda **k: (_ for _ in ()).throw(AssertionError("should not enqueue")))
    state = {"active": {}}
    summary = {}
    mfr._produce(state, now, summary, dry_run=False)
    assert summary["asked"] == 0


def test_ingest_mints_feedback_from_reply(monkeypatch):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    asked_at = now - timedelta(minutes=10)
    state = {"active": {"qid-1": {"meal_pod_id": "datapod:intention.meal:t1", "dish": "Tacos"}}}

    monkeypatch.setattr(mfr, "_question_status", lambda qid: ("asked", asked_at))
    monkeypatch.setattr(mfr, "_first_user_reply_after", lambda a, u: "It was great, kids loved it")

    minted = {}
    monkeypatch.setattr(
        "app.assistant.subconscious.feedback_service.mint_feedback_comment",
        lambda *, target_pod_id, text, actor: (minted.update(target=target_pod_id, text=text, actor=actor) or "fb-1"),
    )
    dismissed = []
    monkeypatch.setattr(
        "app.assistant.pending_questions.store.mark_dismissed",
        lambda qid, *, reason="": (dismissed.append((qid, reason)) or True),
    )

    summary = {}
    mfr._ingest(state, now, summary, dry_run=False)

    assert summary["ingested"] == 1, summary
    assert minted["target"] == "datapod:intention.meal:t1"
    assert "great" in minted["text"]
    assert minted["actor"] == "user"
    assert dismissed and dismissed[0] == ("qid-1", "answered_via_chat")
    assert "qid-1" not in state["active"]          # cleared so it isn't re-ingested


def test_ingest_drops_after_window_with_no_reply(monkeypatch):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    asked_at = now - timedelta(hours=mfr.ANSWER_WINDOW_HOURS + 1)   # window passed
    state = {"active": {"qid-9": {"meal_pod_id": "datapod:intention.meal:t9"}}}
    monkeypatch.setattr(mfr, "_question_status", lambda qid: ("asked", asked_at))
    monkeypatch.setattr(mfr, "_first_user_reply_after", lambda a, u: None)
    dismissed = []
    monkeypatch.setattr(
        "app.assistant.pending_questions.store.mark_dismissed",
        lambda qid, *, reason="": (dismissed.append((qid, reason)) or True),
    )
    monkeypatch.setattr(
        "app.assistant.subconscious.feedback_service.mint_feedback_comment",
        lambda **k: (_ for _ in ()).throw(AssertionError("should not mint with no reply")),
    )
    summary = {}
    mfr._ingest(state, now, summary, dry_run=False)
    assert dismissed and dismissed[0] == ("qid-9", "unanswered_window_passed")
    assert "qid-9" not in state["active"]


def test_ingest_keeps_question_not_yet_asked(monkeypatch):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    state = {"active": {"qid-5": {"meal_pod_id": "datapod:intention.meal:t5"}}}
    monkeypatch.setattr(mfr, "_question_status", lambda qid: ("pending", None))
    summary = {}
    mfr._ingest(state, now, summary, dry_run=False)
    assert "qid-5" in state["active"]              # still waiting for the bridge to ask it
    assert summary["ingested"] == 0
