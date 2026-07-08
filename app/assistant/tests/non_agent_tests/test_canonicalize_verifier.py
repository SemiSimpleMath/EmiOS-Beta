"""Hermetic guard for the pairwise-verifier belief canonicalizer (2026-06-16 rewrite).

No chroma / DB / LLM — fakes for the store, embeddings, and the merge_verifier agent. Proves
the load-bearing logic of `_run_verifier_dedup_pass`:
  - embedding-NN proposes pairs >= MERGE_THRESHOLD; the verifier decides each,
  - a verified merge keeps the higher-observation survivor and rewrites its statement to the
    verifier's canonical_statement (superset-safe), deprecating the loser,
  - the verifier's "not same" verdict keeps look-alikes apart (hydration != finger-stretch),
  - union-find collapses a 3-way cluster in ONE pass (2 merges, not 3),
  - focus_keys (new_only mode) restricts proposals to pairs touching a new belief.
"""
from __future__ import annotations

from types import SimpleNamespace

import belief_engine.pipeline.steps.canonicalize_belief_set as C


# Unit-ish embedding vectors: within-topic pairs sit well above MERGE_THRESHOLD (0.80),
# cross-topic pairs near-orthogonal (below it, never proposed).
_VECS = {
    "food.salmiakki.a":      [1.0, 0.0, 0.0, 0.0],
    "food.salmiakki.b":      [0.99, 0.10, 0.0, 0.0],     # ~0.995 with .a
    "routine.hydration":     [0.0, 1.0, 0.0, 0.0],
    "routine.fingerstretch": [0.0, 0.985, 0.17, 0.0],    # ~0.985 with hydration -> proposed
    "home.dish1":            [0.0, 0.0, 0.0, 1.0],
    "home.dish2":            [0.0, 0.0, 0.05, 0.998],     # ~0.999 with dish1
    "home.dish3":            [0.0, 0.03, 0.02, 0.999],    # ~0.999 with dish1
}


def _belief(key, stmt, obs):
    return SimpleNamespace(
        id=key, belief_key=key, statement=stmt, confidence="high",
        scope="chronic", status="active", observation_count=obs, domain="test", kind=None,
    )


def _make_beliefs():
    return [
        _belief("food.salmiakki.a", "the user's favorite Finnish snack is salmiakki.", 5),
        _belief("food.salmiakki.b",
                "Salmiakki is the user's favorite snack; avoid suggesting it when his stomach is upset.", 2),
        _belief("routine.hydration", "Hydration reminders are currently too frequent.", 3),
        _belief("routine.fingerstretch", "Finger-stretch reminders are currently too frequent.", 3),
        _belief("home.dish1", "Run the dishwasher at night.", 4),
        _belief("home.dish2", "Run the dishwasher after 9 PM.", 1),
        _belief("home.dish3", "Start the dishwasher late at night.", 1),
    ]


def _topic(s: str) -> str:
    s = s.lower()
    for t in ("salmiakki", "dishwasher", "hydration", "finger", "carbonara"):
        if t in s:
            return t
    return s


class _FakeChroma:
    def __init__(self, vecs):
        self._vecs = vecs

    def get_all_for_domain(self, domain):
        return list(self._vecs.items())


class _FakeStore:
    """Minimal BeliefStore stand-in: serves embeddings, looks up by key, applies merges."""
    def __init__(self, beliefs, vecs):
        self._chroma = _FakeChroma(vecs)
        self.by_key = {b.belief_key: b for b in beliefs}
        self.merges = []

    def list_by_domain(self, domain, *, status="active"):
        return [b for b in self.by_key.values() if b.status == status]

    def get_by_key(self, key):
        return self.by_key.get(key)

    def merge_belief(self, *, surviving_key, surviving_statement, surviving_confidence,
                     surviving_scope, deprecated_keys, domain, merge_reasoning):
        self.by_key[surviving_key].statement = surviving_statement
        for dk in deprecated_keys:
            self.by_key[dk].status = "deprecated"
        self.merges.append((surviving_key, list(deprecated_keys), surviving_statement))


class _FakeAgent:
    def __init__(self):
        self.calls = 0

    def action_handler(self, msg):
        self.calls += 1
        ai = msg.agent_input
        a, b = ai["phrase_a"], ai["phrase_b"]
        same = _topic(a) == _topic(b)
        canon = a if len(a) >= len(b) else b   # fuller statement wins
        return SimpleNamespace(data={
            "same": same,
            "reason": f"{_topic(a)} vs {_topic(b)}",
            "canonical_statement": canon if same else "",
        })


class _FakeFactory:
    """Holds ONE agent instance so verifier-call counts persist across passes."""
    def __init__(self):
        self.agent = _FakeAgent()

    def create_agent(self, name):
        return self.agent


def _run(beliefs, vecs=_VECS, focus_keys=None, *, monkeypatch, tmp_path, factory=None):
    """Run one dedup pass with the verdict store pointed at an isolated tmp sqlite file
    (never the real emi.db). Returns (store, pass_result, factory)."""
    verdict_db = str(tmp_path / "verdicts.db")
    monkeypatch.setattr(C.verdicts, "belief_db_path", lambda: verdict_db)
    factory = factory or _FakeFactory()
    store = _FakeStore(beliefs, vecs)
    pass_result = C._run_verifier_dedup_pass(
        beliefs, "test", store, factory, scope_context=None, focus_keys=focus_keys,
    )
    return store, pass_result, factory


def test_full_pass_merges_dups_and_keeps_lookalikes_apart(monkeypatch, tmp_path):
    beliefs = _make_beliefs()
    store, res, _ = _run(beliefs, monkeypatch=monkeypatch, tmp_path=tmp_path)

    # salmiakki ×2 -> 1 ; dishwasher ×3 -> 1 (2 merges via union-find) ; = 3 merges total
    assert res["merges"] == 3

    # salmiakki: higher-obs .a survives, its statement REWRITTEN to the fuller (.b) text.
    assert store.by_key["food.salmiakki.a"].status == "active"
    assert store.by_key["food.salmiakki.b"].status == "deprecated"
    assert "stomach is upset" in store.by_key["food.salmiakki.a"].statement   # superset preserved

    # look-alikes kept apart — the verifier said NOT same despite ~0.985 cosine.
    assert store.by_key["routine.hydration"].status == "active"
    assert store.by_key["routine.fingerstretch"].status == "active"

    # 3-way dishwasher cluster collapses to the highest-obs survivor in ONE pass.
    assert store.by_key["home.dish1"].status == "active"
    assert store.by_key["home.dish2"].status == "deprecated"
    assert store.by_key["home.dish3"].status == "deprecated"

    active = [k for k, b in store.by_key.items() if b.status == "active"]
    assert sorted(active) == ["food.salmiakki.a", "home.dish1",
                              "routine.fingerstretch", "routine.hydration"]


def test_focus_keys_restricts_proposals_to_new_belief(monkeypatch, tmp_path):
    # new_only mode: only the salmiakki.b belief is "new" -> only pairs touching it are
    # proposed, so salmiakki merges but the dishwasher cluster is left untouched this run.
    beliefs = _make_beliefs()
    store, res, _ = _run(beliefs, focus_keys={"food.salmiakki.b"},
                         monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert res["merges"] == 1
    assert store.by_key["food.salmiakki.b"].status == "deprecated"
    assert store.by_key["home.dish2"].status == "active"   # not touched — no focus key in pair
    assert store.by_key["home.dish3"].status == "active"


_KW_VECS = {
    "food.pasta.a": [1.0, 0.0, 0.0],
    "food.pasta.b": [0.65, 0.76, 0.0],  # cos ~0.65 with .a: below the 0.80 embedding threshold
                                         # (so embedding never proposes it) but above the keyword
                                         # cosine floor, so the keyword channel does.
    "home.dishX":   [0.0, 0.0, 1.0],
}


def test_keyword_channel_catches_embedding_miss(monkeypatch, tmp_path):
    # .a and .b embed at ~0.65 — below the 0.80 embedding threshold, so the embedding channel
    # never proposes them — but they share the rare word "carbonara" and clear the keyword cosine
    # floor, so the keyword channel does. Proves the second recall channel reaches dups embedding misses.
    beliefs = [
        _belief("food.pasta.a", "Carbonara is one of the user's strongest pasta favorites.", 3),
        _belief("food.pasta.b", "For a reliable go-to dinner, lean toward making carbonara.", 1),
        _belief("home.dishX", "Run the dishwasher at night.", 2),
    ]
    store, res, _ = _run(beliefs, _KW_VECS, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert res["merges"] == 1                                   # carbonara pair merged via keyword
    assert store.by_key["food.pasta.b"].status == "deprecated"
    assert store.by_key["food.pasta.a"].status == "active"     # higher-obs survivor
    assert store.by_key["home.dishX"].status == "active"       # unrelated, untouched


# --- Durability: recorded verdicts skip, statement changes re-judge, the cap truncates ------


def test_not_same_verdicts_are_recorded_and_skipped_next_pass(monkeypatch, tmp_path):
    # Pass 1: hydration vs finger-stretch is proposed (~0.985 cosine) and judged NOT same.
    beliefs = [
        _belief("routine.hydration", "Hydration reminders are currently too frequent.", 3),
        _belief("routine.fingerstretch", "Finger-stretch reminders are currently too frequent.", 3),
    ]
    _, res1, factory = _run(beliefs, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert res1["merges"] == 0
    assert res1["verifier_calls"] == 1
    assert factory.agent.calls == 1

    # Pass 2, same statements: the recorded verdict settles the pair — zero LLM calls.
    _, res2, _ = _run(beliefs, monkeypatch=monkeypatch, tmp_path=tmp_path, factory=factory)
    assert res2["verifier_calls"] == 0
    assert res2["skipped_distinct"] == 1
    assert factory.agent.calls == 1   # unchanged

    # Pass 3, one statement evolved: the verdict is invalidated — the pair is re-judged.
    beliefs[0].statement = "Hydration reminders should fire at most twice a day."
    _, res3, _ = _run(beliefs, monkeypatch=monkeypatch, tmp_path=tmp_path, factory=factory)
    assert res3["skipped_distinct"] == 0
    assert res3["verifier_calls"] == 1
    assert factory.agent.calls == 2


def test_call_cap_truncates_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "MAX_VERIFIER_CALLS_PER_RUN", 1)
    beliefs = _make_beliefs()   # would need 4+ verifier calls uncapped
    _, res, factory = _run(beliefs, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert res["truncated"] is True
    assert res["verifier_calls"] == 1
    assert factory.agent.calls == 1
