"""Guard: the feedback_extractor must not fabricate phantom beliefs.

When the user says "kids don't like zucchini," the extractor emits two extractions:
a 'confirms' on the dislike belief AND a 'contradicts' on a mirror "kids WILL eat
zucchini" belief. The old persist upserted BOTH, so contradicting a non-existent
mirror CREATED it — an affirmative belief carrying only negative evidence. That
manufactured 3 phantoms in production (zucchini / Thai Spice / weekday breakfast);
see scratch/MEAL-PLANNING-AUDIT.md.

Fix: a 'contradicts' extraction only weakens an EXISTING belief (add_evidence_to_
existing); if none exists it is skipped, never minted. This pins that. Uses fakes —
no DB, no live belief store.
"""
from __future__ import annotations

import app.assistant.subconscious.feedback_extractor_persist as fep


class _Rec:
    def __init__(self, key):
        self.id = "id-" + key
        self.belief_key = key


class _FakeStore:
    """Stands in for BeliefStore: upsert_belief creates; add_evidence_to_existing
    only succeeds for a key already present."""

    def __init__(self):
        self.existing = {"meal.real.existing"}   # a pre-existing belief to weaken
        self.upserted = []
        self.weakened = []

    def upsert_belief(self, req, evidence=None):
        self.upserted.append(req.belief_key)
        self.existing.add(req.belief_key)
        return _Rec(req.belief_key)

    def add_evidence_to_existing(self, belief_key, evidence):
        if belief_key not in self.existing:
            return None
        self.weakened.append(belief_key)
        return _Rec(belief_key)


class _FakePodStore:
    def get(self, _pod_id):
        return None

    def put(self, _pod):
        pass


def test_contradicts_never_mints_a_phantom(monkeypatch):
    fake = _FakeStore()
    # persist imports BeliefStore from belief_engine at call time -> patch the source.
    monkeypatch.setattr("belief_engine.store.belief_store.BeliefStore", lambda: fake)
    monkeypatch.setattr(fep, "PodStore", lambda: _FakePodStore())

    out = fep.apply_feedback_extractor_output({
        "extractions": [
            # the real signal — must be upserted
            {"signal_type": "confirms", "belief_key": "meal.kids.dislike_soup",
             "statement": "Kids dislike soup", "domain": "meal", "confidence": "high",
             "source_comment_pod_id": "c1"},
            # mirror of the same statement against a belief that does NOT exist -> phantom
            {"signal_type": "contradicts", "belief_key": "meal.kids.will_eat_zucchini",
             "statement": "Children will eat zucchini", "domain": "meal", "confidence": "high",
             "source_comment_pod_id": "c1"},
            # contradiction of a belief that DOES exist -> legitimately weakens it
            {"signal_type": "contradicts", "belief_key": "meal.real.existing",
             "statement": "irrelevant", "domain": "meal", "confidence": "high",
             "source_comment_pod_id": "c1"},
        ],
        "skipped": [],
    })

    # 'confirms' belief is created
    assert "meal.kids.dislike_soup" in fake.upserted, out
    # the phantom mirror is NEVER created
    assert "meal.kids.will_eat_zucchini" not in fake.upserted, out
    assert out["phantom_skipped_count"] == 1, out
    # the contradiction against the existing belief weakens it, not mints it
    assert "meal.real.existing" in fake.weakened, out
    assert "meal.real.existing" not in fake.upserted, out
