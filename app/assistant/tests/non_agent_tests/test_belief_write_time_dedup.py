"""Hermetic guard for the global belief pass (2026-09-10) and the relation verifier (2026-09-11).

No chroma / DB / LLM. Proves the behaviours that ended the per-domain fan-out, plus the
relations that replaced the verifier's old same/not-same boolean:

  - `dedup_candidate`: a NEW statement is compared with its nearest active beliefs and the
    merge_verifier decides once per candidate, nearest first; a "same" verdict hands back the
    existing belief (the create becomes an update) with the verifier's canonical statement,
    inert verdicts are handed back for recording, locked / same-key hits are skipped.
  - a `supersedes` verdict deprecates whichever side the dated evidence calls outdated;
  - a `contradicts` verdict contests the stored belief so the reevaluator rules on its trail;
  - each side reaches the verifier with its dated evidence, which is the only thing that can
    separate those two relations.
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
        first_observed="2026-01-04", last_confirmed="2026-08-30",
    )


def _evidence(date, summary, signal="observed"):
    return SimpleNamespace(source_date=date, created_at=f"{date}T09:00:00Z",
                           signal_type=signal, summary=summary)


class _FakeStore:
    def __init__(self, hits, evidence=None):
        self._hits = hits                    # list of (belief, score) returned by find_similar
        self._evidence = evidence or {}      # belief.id -> [evidence]
        self.queries = []
        self.deprecated = []
        self.contested = []

    def find_similar(self, query, *, k, threshold, domain=None):
        self.queries.append((query, k, threshold, domain))
        return [(b, s) for b, s in self._hits if s >= threshold]

    def get_evidence(self, belief_id):
        return list(self._evidence.get(belief_id, []))

    def deprecate(self, belief_key, *, reason=""):
        self.deprecated.append((belief_key, reason))

    def mark_contested(self, belief_key):
        self.contested.append(belief_key)


def _topic(s: str) -> str:
    s = s.lower()
    for t in ("salmiakki", "hydration", "finger", "honey"):
        if t in s:
            return t
    return s


class _FakeVerifier:
    """relation='same' exactly when both statements are about the same topic word, unless a
    scripted relation is supplied for the whole run."""
    def __init__(self, relation=None, current_side="", data_override=None):
        self.calls = []
        self._relation = relation
        self._current_side = current_side
        self._data_override = data_override

    def action_handler(self, msg):
        ai = msg.agent_input
        a, b = ai["phrase_a"], ai["phrase_b"]
        self.calls.append(ai)
        if self._data_override is not None:
            return SimpleNamespace(data=dict(self._data_override))
        if self._relation is not None:
            return SimpleNamespace(data={
                "relation": self._relation, "reason": "scripted",
                "canonical_statement": "", "current_side": self._current_side,
            })
        same = _topic(a) == _topic(b)
        return SimpleNamespace(data={
            "relation": "same" if same else "different",
            "reason": "topic match" if same else "different topic",
            "canonical_statement": (a if len(a) >= len(b) else b) if same else "",
            "current_side": "",
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
    assert distinct[0][1].startswith("different: ")   # the relation is kept in the recorded reason
    assert len(verifier.calls) == 1


def test_specialises_leaves_both_standing_and_is_recorded_with_its_relation():
    broad = _belief("food.tea", "The user prefers tea in the morning.")
    store = _FakeStore([(broad, 0.86)])
    verifier = _FakeVerifier(relation="specialises")
    folded, _, distinct = U.dedup_candidate(
        store, verifier, statement="The user prefers coffee when travelling for work.",
        belief_key="food.coffee.travel", scope_context=None,
    )
    assert folded is None
    assert store.deprecated == [] and store.contested == []
    assert [c.belief_key for c, _ in distinct] == ["food.tea"]
    assert distinct[0][1].startswith("specialises: ")


def test_supersedes_deprecates_the_stored_belief_when_the_new_one_is_current():
    stale = _belief("food.honey", "The user likes honey in tea.")
    store = _FakeStore([(stale, 0.91)])
    verifier = _FakeVerifier(relation="supersedes", current_side="a")
    folded, _, distinct = U.dedup_candidate(
        store, verifier, statement="The user dislikes honey and avoids it in tea.",
        belief_key="food.honey.dislike", scope_context=None,
    )
    # The create still proceeds; the belief it replaced is retired rather than left beside it.
    assert folded is None and distinct == []
    assert [k for k, _ in store.deprecated] == ["food.honey"]
    assert "food.honey.dislike" in store.deprecated[0][1]
    assert store.contested == []


def test_supersedes_leaves_the_stored_belief_alone_when_it_is_the_current_one():
    current = _belief("food.honey", "The user dislikes honey and avoids it in tea.")
    store = _FakeStore([(current, 0.91)])
    verifier = _FakeVerifier(relation="supersedes", current_side="b")
    folded, _, distinct = U.dedup_candidate(
        store, verifier, statement="The user likes honey in tea.",
        belief_key="food.honey.like", scope_context=None,
    )
    assert folded is None and distinct == []
    assert store.deprecated == []      # never deprecate the side the evidence calls current
    assert store.contested == []


def test_contradicts_contests_the_stored_belief_for_the_reevaluator():
    other = _belief("food.honey", "The user likes honey in tea.")
    store = _FakeStore([(other, 0.9)])
    verifier = _FakeVerifier(relation="contradicts")
    folded, _, distinct = U.dedup_candidate(
        store, verifier, statement="The user dislikes honey and avoids it in tea.",
        belief_key="food.honey.dislike", scope_context=None,
    )
    assert folded is None
    assert store.contested == ["food.honey"]
    assert store.deprecated == []
    # An unresolved conflict is NOT filed as a settled verdict — it must be asked again.
    assert distinct == []


def test_unusable_relation_changes_nothing():
    other = _belief("food.honey", "The user likes honey in tea.")
    store = _FakeStore([(other, 0.9)])
    verifier = _FakeVerifier(data_override={"relation": "maybe-ish", "reason": "unsure"})
    folded, _, distinct = U.dedup_candidate(
        store, verifier, statement="The user dislikes honey.",
        belief_key="food.honey.dislike", scope_context=None,
    )
    assert folded is None                       # never guessed into a merge
    assert store.deprecated == [] and store.contested == []
    assert [c.belief_key for c, _ in distinct] == ["food.honey"]   # falls back to inert


def test_the_verifier_sees_each_side_s_dated_evidence():
    stored = _belief("food.honey", "The user likes honey in tea.")
    store = _FakeStore(
        [(stored, 0.9)],
        evidence={stored.id: [
            _evidence("2026-02-11", "asked for honey with the evening tea"),
            _evidence("2026-08-30", "kept honey on the shopping list"),
        ]},
    )
    verifier = _FakeVerifier(relation="different")
    U.dedup_candidate(store, verifier, statement="The user dislikes honey.",
                      belief_key="food.honey.dislike", scope_context=None)

    ctx_b = verifier.calls[0]["context_b"]
    assert "2026-08-30" in ctx_b and "shopping list" in ctx_b
    assert "observed 3x" in ctx_b and "food" in ctx_b
    # The incoming statement has no stored history, and says so rather than looking unsupported.
    assert verifier.calls[0]["context_a"] == U._NEW_STATEMENT_CONTEXT


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
