"""Hermetic guard for the global belief pass (2026-09-10).

No chroma / DB / LLM. Proves the two behaviours that ended the per-domain fan-out:

  - `dedup_candidate`: a NEW statement is compared with its nearest active beliefs and the
    merge_verifier decides once per candidate, nearest first; a "same" verdict hands back the
    existing belief (the create becomes an update) with the verifier's canonical statement,
    "not the same" verdicts are handed back for recording, locked / same-key hits are skipped.
  - `resolve_domain`: an existing belief keeps its area; a new belief takes the updater's
    `domain` when known, else its key prefix when known, else None (refused loudly).
"""
from __future__ import annotations

from types import SimpleNamespace

import belief_engine.pipeline.steps.update_beliefs as U


def _belief(key, stmt, domain="food", locked=0, obs=3):
    return SimpleNamespace(
        id=f"id-{key}", belief_key=key, statement=stmt, confidence="high", scope="chronic",
        status="active", observation_count=obs, domain=domain, kind=None, locked=locked,
    )


class _FakeStore:
    def __init__(self, hits):
        self._hits = hits          # list of (belief, score) returned by find_similar
        self.queries = []

    def find_similar(self, query, *, k, threshold, domain=None):
        self.queries.append((query, k, threshold, domain))
        return [(b, s) for b, s in self._hits if s >= threshold]


def _topic(s: str) -> str:
    s = s.lower()
    for t in ("salmiakki", "hydration", "finger"):
        if t in s:
            return t
    return s


class _FakeVerifier:
    """same=True exactly when both statements are about the same topic word."""
    def __init__(self):
        self.calls = []

    def action_handler(self, msg):
        a, b = msg.agent_input["phrase_a"], msg.agent_input["phrase_b"]
        self.calls.append((a, b))
        same = _topic(a) == _topic(b)
        return SimpleNamespace(data={
            "same": same, "reason": "topic match" if same else "different topic",
            "canonical_statement": (a if len(a) >= len(b) else b) if same else "",
        })


def test_same_verdict_folds_the_create_into_the_existing_belief():
    existing = _belief("food.salmiakki", "The user's favorite Finnish snack is salmiakki.")
    store = _FakeStore([(existing, 0.93)])
    verifier = _FakeVerifier()
    folded, canonical, distinct = U.dedup_candidate(
        store, verifier, statement="Salmiakki is the snack the user likes best.",
        belief_key="general.snack.salmiakki", scope_context=None,
    )
    assert folded is existing
    assert canonical == existing.statement   # the fake returns the fuller text
    assert distinct == []
    assert len(verifier.calls) == 1
    assert store.queries[0][1] == U._DEDUP_K and store.queries[0][2] == U.MERGE_THRESHOLD


def test_not_same_verdicts_come_back_for_recording_and_the_create_proceeds():
    lookalike = _belief("routine.hydration", "Hydration reminders are too frequent.", domain="routine")
    store = _FakeStore([(lookalike, 0.85)])
    verifier = _FakeVerifier()
    folded, canonical, distinct = U.dedup_candidate(
        store, verifier, statement="Finger-stretch reminders are too frequent.",
        belief_key="routine.fingerstretch", scope_context=None,
    )
    assert folded is None and canonical is None
    assert [c.belief_key for c, _ in distinct] == ["routine.hydration"]
    assert len(verifier.calls) == 1


def test_locked_and_same_key_neighbours_cost_no_verifier_call():
    locked = _belief("food.salmiakki", "Salmiakki is the user's favorite snack.", locked=1)
    same_key = _belief("food.new", "Salmiakki again.")
    store = _FakeStore([(locked, 0.95), (same_key, 0.9)])
    verifier = _FakeVerifier()
    folded, _, distinct = U.dedup_candidate(
        store, verifier, statement="Salmiakki is the best snack.", belief_key="food.new",
        scope_context=None,
    )
    assert folded is None and distinct == []
    assert verifier.calls == []


def test_resolve_domain_rules():
    valid = ["routine", "health", "food", "general"]
    existing = _belief("health.sleep", "x", domain="health")
    # an existing belief keeps its area even if the updater says otherwise
    assert U.resolve_domain(agent_domain="food", belief_key="health.sleep", existing=existing,
                            valid_domains=valid) == "health"
    # a new belief takes the updater's known area
    assert U.resolve_domain(agent_domain="Food", belief_key="misc.x", existing=None,
                            valid_domains=valid) == "food"
    # else the key prefix when known
    assert U.resolve_domain(agent_domain="kitchen", belief_key="routine.dogs", existing=None,
                            valid_domains=valid) == "routine"
    # else refused
    assert U.resolve_domain(agent_domain="kitchen", belief_key="misc.dogs", existing=None,
                            valid_domains=valid) is None
    # a per-domain slice files everything under that domain
    assert U.resolve_domain(agent_domain="food", belief_key="misc.dogs", existing=None,
                            valid_domains=valid, forced="routine") == "routine"
