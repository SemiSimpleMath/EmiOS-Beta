"""Regression guard for the wellness/romantic intention-pod persisters.

Both `_mint_intention_{wellness,romantic}_pod` referenced an undefined
`addresses` variable inside a try/except that swallowed the error and
returned None — so EVERY wellness and romantic proposal pod was silently
dropped (the set pod ended up with zero refs). Mirrors the meal-lane fix.

These tests stub PodStore so no real pods are written; they assert
(1) a proposal now mints a pod, and (2) a mint failure is now LOUD
(propagates) rather than swallowed.
"""
from __future__ import annotations

import pytest

from app.assistant.subconscious import romantic_persist, wellness_persist


class _FakeStore:
    """No-op PodStore stub — Pod construction still runs (and validates),
    only the DB write is skipped."""
    def __init__(self, *a, **k):
        self.put_calls = 0

    def put(self, pod):
        self.put_calls += 1


class _RaisingStore:
    def __init__(self, *a, **k):
        pass

    def put(self, pod):
        raise RuntimeError("simulated pod_store write failure")


_WELLNESS_PROPOSAL = {
    "kind": "workout",
    "actors": ["the user"],
    "date": "2026-06-17",
    "summary": "30 min easy bike ride",
    "source": "routine_maintenance",
    "confidence": "high",
    "novelty": "familiar",
    "reasoning": "Active recovery after a rest day.",
}

_ROMANTIC_PROPOSAL = {
    "kind": "small_gesture",
    "actors": ["the user", "the user's partner"],
    "date": "2026-06-17",
    "summary": "Pick up favorite coffee on the way home",
    "source": "routine_maintenance",
    "confidence": "high",
    "novelty": "familiar",
    "reasoning": "Low-effort, high-warmth gesture.",
}


def test_wellness_proposal_mints_a_pod(monkeypatch):
    monkeypatch.setattr(wellness_persist, "PodStore", _FakeStore)
    out = wellness_persist.apply_wellness_proposer_output(
        {"proposals": [_WELLNESS_PROPOSAL]}
    )
    assert out["proposal_pod_count"] == 1
    # Two-axis model: kind=intention in the id; the #wellness variant rides as a tag.
    assert out["proposal_pod_ids"][0].startswith("datapod:intention:")
    assert out["set_pod_id"].startswith("datapod:intention:")


def test_romantic_proposal_mints_a_pod(monkeypatch):
    monkeypatch.setattr(romantic_persist, "PodStore", _FakeStore)
    out = romantic_persist.apply_romantic_proposer_output(
        {"proposals": [_ROMANTIC_PROPOSAL]}
    )
    assert out["proposal_pod_count"] == 1
    assert out["proposal_pod_ids"][0].startswith("datapod:intention:")
    assert out["set_pod_id"].startswith("datapod:intention:")


def test_wellness_mint_failure_is_loud(monkeypatch):
    """A write failure must propagate, not be swallowed into a dropped pod."""
    monkeypatch.setattr(wellness_persist, "PodStore", _RaisingStore)
    with pytest.raises(RuntimeError):
        wellness_persist.apply_wellness_proposer_output(
            {"proposals": [_WELLNESS_PROPOSAL]}
        )


def test_romantic_mint_failure_is_loud(monkeypatch):
    monkeypatch.setattr(romantic_persist, "PodStore", _RaisingStore)
    with pytest.raises(RuntimeError):
        romantic_persist.apply_romantic_proposer_output(
            {"proposals": [_ROMANTIC_PROPOSAL]}
        )
