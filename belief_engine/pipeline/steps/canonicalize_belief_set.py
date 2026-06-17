"""
Step 4: Canonicalize near-duplicate beliefs within a domain.

Embedding nearest-neighbour PROPOSES candidate duplicate PAIRS over the active belief set;
the `belief_engine::merge_verifier` LLM DECIDES each pair. The verifier is asymmetric — a
wrong merge silently destroys a distinct belief, so it merges only when certain the two are
the same belief about the same specific thing, and it returns the reconciled
`canonical_statement`. A verified merge rewrites the survivor's statement to that canonical
statement and deprecates the loser (`store.merge_belief`), so a superset ("A plus an extra
clause") collapses WITHOUT dropping the extra clause. Nothing merges on similarity alone —
the verifier decides every pair.

This replaced the chunk-based canonicalizer (2026-06-16). The old approach sent 40-belief
windows to an LLM and could miss dups split across windows; its hot path also compared NEW
beliefs only against each other, so a new duplicate of an OLD belief leaked until the weekly
full sweep. Pairwise + whole-set NN fixes both: a focused per-pair decision, and (in
new_only mode) each new belief is compared against the ENTIRE active set.

Two modes (decided by belief_engine.state.sweep_tracker.decide_mode):
  - "full" (weekly): propose pairs across ALL active beliefs in the domain.
  - "new_only" (nightly): propose only pairs that involve a belief created since the last
    full sweep — but against the whole active set — so a new dup of an old belief is caught.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.utils.pydantic_classes import Message

from belief_engine.state.sweep_tracker import decide_mode, read_last_full_sweep_at
from belief_engine.store.belief_store import BeliefRecord, BeliefStore

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

logger = logging.getLogger(__name__)

_AGENT_NAME = "belief_engine::merge_verifier"

# Embedding cosine at/above which a PAIR is proposed to the verifier. Recall-biased on
# purpose — the verifier is the precision gate, so propose generously and let it reject.
MERGE_THRESHOLD = 0.80
# Backstop on verifier calls per pass (strongest pairs first). High enough not to bite the real
# candidate count (a global sweep is ~1-2k pairs); only a pathological explosion would hit it.
MAX_PAIRS = 4000

# Second recall channel — shared distinctive keyword. Pairs of beliefs that share a word whose
# document frequency across the set is within [MIN, MAX] are also proposed: a lexical complement to
# embedding NN that catches divergent-phrasing dups embedding misses ("standing-break nudges" vs
# "standing break reminders"). The DF band excludes unique words (DF 1 — nothing to pair) and
# corpus-common/topical words (high DF — those clusters are embedding's job, and big blocks explode).
# Commonness is measured from the corpus (DF), not enumerated; _STOP only pre-drops universal glue.
KEYWORD_DF_MIN = 2
KEYWORD_DF_MAX = 12
# Keyword pairs are proposed only when ALSO at least this cosine-similar — drops coincidental
# single-word overlaps so the verifier isn't spent on them. Sits below MERGE_THRESHOLD, so the
# keyword channel adds exactly the "related but embedded just below the threshold" band (e.g. the
# ~0.6 "standing-break nudges" vs "standing break reminders" pair).
KEYWORD_MIN_COSINE = 0.50
_STOP = frozenset((
    "the a an this that these those of to in on at by for with from into over under and or but if "
    "then else when while is are was were be been being have has had do does did not no nor it its "
    "as also more most some any all your his her their our about can could will would should may "
    "might must"
).split())


def _tokens(text: str) -> set:
    """Distinctive word-stems in a statement: lowercase alpha tokens minus generic glue words,
    lightly stemmed (plurals / -es / -ies). The DF band — not this list — handles corpus-common
    words like 'reminder' or a name that appears everywhere."""
    out: set = set()
    for w in re.findall(r"[a-z][a-z']{2,}", (text or "").lower()):
        w = w.replace("'", "")
        if len(w) < 3 or w in _STOP:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 4 and w.endswith("es"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def _keyword_pairs(items) -> Set:
    """Candidate pairs from the keyword channel: beliefs sharing a word whose document frequency
    across `items` falls within [KEYWORD_DF_MIN, KEYWORD_DF_MAX]. Returns a set of (i, j), i < j."""
    from collections import Counter, defaultdict
    toks = [_tokens(b.statement) for b in items]
    df: Counter = Counter()
    for t in toks:
        for w in t:
            df[w] += 1
    keys = {w for w, c in df.items() if KEYWORD_DF_MIN <= c <= KEYWORD_DF_MAX}
    blocks: Dict[str, List[int]] = defaultdict(list)
    for i, t in enumerate(toks):
        for w in (t & keys):
            blocks[w].append(i)   # appended in increasing i, so members stay sorted
    pairs: Set = set()
    for members in blocks.values():
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                pairs.add((members[a], members[b]))
    return pairs


def _embeddings_for(store: BeliefStore, beliefs: List[BeliefRecord]):
    """(items, normalized matrix) for the beliefs that have an embedding + a statement."""
    if np is None:
        raise RuntimeError("canonicalize requires numpy")
    id_to_vec = dict(store._chroma.get_all_for_domain(beliefs[0].domain)) if beliefs else {}
    items = [b for b in beliefs if b.id in id_to_vec and (b.statement or "").strip()]
    if len(items) < 2:
        return [], None
    mat = np.asarray([id_to_vec[b.id] for b in items], dtype=float)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return items, mat / np.where(norms > 0, norms, 1.0)


def _find_duplicate_clusters(store: BeliefStore, domain: str) -> List[List[BeliefRecord]]:
    """Embedding-only PREVIEW of candidate duplicate clusters (no LLM). Pairs with cosine
    >= MERGE_THRESHOLD form edges; connected components of size >= 2 are returned. The live
    pass still asks the verifier about each pair — this is just a cheap 'what's in scope' view
    (used by scripts/dry_run_canonicalize)."""
    items, mat = _embeddings_for(store, store.list_by_domain(domain))
    if not items:
        return []
    sims = mat @ mat.T
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    n = len(items)
    for i in range(n):
        for j in range(i + 1, n):
            if float(sims[i, j]) >= MERGE_THRESHOLD:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: Dict[int, List[BeliefRecord]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(items[idx])
    return [g for g in groups.values() if len(g) >= 2]


def _verify_same(agent, a: BeliefRecord, b: BeliefRecord, *, scope_context) -> Dict[str, Any]:
    """Ask the merge_verifier whether two beliefs are the same; return its structured output
    ({same, reason, canonical_statement}) as a dict."""
    resp = agent.action_handler(Message(
        agent_input={
            "phrase_a": a.statement,
            "phrase_b": b.statement,
            "ctype": "belief",
            "ctype_noun": "concept",
        },
        scope_context=scope_context,
    ))
    data = resp.data if resp and hasattr(resp, "data") else {}
    if isinstance(data, dict):
        return data
    return {
        "same": getattr(data, "same", None),
        "reason": getattr(data, "reason", ""),
        "canonical_statement": getattr(data, "canonical_statement", ""),
    }


def _run_verifier_dedup_pass(
    beliefs: List[BeliefRecord],
    domain: str,
    store: BeliefStore,
    agent_factory: Any,
    *,
    scope_context: Any,
    focus_keys: Optional[Set[str]] = None,
) -> int:
    """Pairwise embedding-NN proposes candidate dup pairs; the verifier decides each; a verified
    merge rewrites the survivor to the reconciled statement and deprecates the loser.

    `focus_keys` (new_only mode): only propose pairs where at least one belief's key is in the
    set — i.e. compare new beliefs against the whole set without re-checking every old pair.
    None = propose all pairs (full sweep). Returns the number of merges performed.
    """
    items, mat = _embeddings_for(store, beliefs)
    if not items:
        return 0
    sims = mat @ mat.T
    n = len(items)

    # Two recall channels propose candidates; the verifier still decides every pair.
    #  (1) embedding NN — semantic (paraphrase / synonyms).
    #  (2) shared distinctive keyword — lexical, catches divergent-phrasing dups that embed below
    #      threshold but share a rare term. High-DF/topical words are left to channel (1).
    candidates: Set = set()
    for i in range(n):
        for j in range(i + 1, n):
            if float(sims[i, j]) >= MERGE_THRESHOLD:
                candidates.add((i, j))
    for i, j in _keyword_pairs(items):
        if float(sims[i, j]) >= KEYWORD_MIN_COSINE:
            candidates.add((i, j))

    # In new_only mode, at least one side of a pair must be a focus (new) belief.
    pairs = []
    for i, j in candidates:
        if focus_keys is not None and items[i].belief_key not in focus_keys \
                and items[j].belief_key not in focus_keys:
            continue
        pairs.append((float(sims[i, j]), i, j))
    pairs.sort(reverse=True)
    pairs = pairs[:MAX_PAIRS]
    if not pairs:
        return 0

    # Local union-find so a belief merged earlier in the pass isn't merged again.
    merged_into: Dict[str, str] = {}

    def root(key: str) -> str:
        seen: Set[str] = set()
        while key in merged_into and key not in seen:
            seen.add(key)
            key = merged_into[key]
        return key

    agent = agent_factory.create_agent(_AGENT_NAME)
    if agent is None:
        raise RuntimeError(f"Agent '{_AGENT_NAME}' not found")

    merges = 0
    for _s, i, j in pairs:
        ka, kb = root(items[i].belief_key), root(items[j].belief_key)
        if ka == kb:
            continue
        ra, rb = store.get_by_key(ka), store.get_by_key(kb)
        if ra is None or rb is None or ra.status != "active" or rb.status != "active":
            continue

        verdict = _verify_same(agent, ra, rb, scope_context=scope_context)
        if not verdict.get("same"):
            continue

        # Survivor = the better-supported belief (keeps the more-observed key + its history).
        # Its statement is REWRITTEN to the verifier's reconciled canonical_statement, so the
        # surviving key is just identity/provenance — content comes from the verifier.
        survivor, loser = (ra, rb) if ra.observation_count >= rb.observation_count else (rb, ra)
        canonical = (verdict.get("canonical_statement") or "").strip() or survivor.statement
        try:
            store.merge_belief(
                surviving_key=survivor.belief_key,
                surviving_statement=canonical,
                surviving_confidence=survivor.confidence,
                surviving_scope=survivor.scope,
                deprecated_keys=[loser.belief_key],
                domain=domain,
                merge_reasoning=(verdict.get("reason") or "")[:300],
            )
            merged_into[loser.belief_key] = survivor.belief_key
            merges += 1
            logger.info("[CanonicalizeBeliefSet] domain=%s merged %s <- %s",
                        domain, survivor.belief_key, loser.belief_key)
        except Exception:
            logger.exception("[CanonicalizeBeliefSet] domain=%s merge failed surviving=%s",
                             domain, survivor.belief_key)

    return merges


def _filter_new_beliefs_since(
    beliefs: List[BeliefRecord],
    cutoff_utc,
) -> List[BeliefRecord]:
    """Return beliefs whose `created_at` is strictly after the cutoff.

    cutoff_utc=None (no prior sweep recorded) → returns everything, so the first run after
    bootstrap canonicalizes the full set. Defensive on parse failure: treats unparseable
    timestamps as "new" (better to include than silently skip).
    """
    if cutoff_utc is None:
        return list(beliefs)
    out: List[BeliefRecord] = []
    cutoff_iso = cutoff_utc.isoformat()
    for b in beliefs:
        created_at = getattr(b, "created_at", None)
        if not created_at:
            out.append(b)
            continue
        s = str(created_at).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if s > cutoff_iso:
            out.append(b)
    return out


class CanonicalizeBeliefSetStep:
    """Step 4 of BeliefEnginePipeline — pairwise-verifier dedup (see module docstring)."""

    name = "canonicalize_belief_set"

    def __init__(self, domain: str) -> None:
        self.domain = domain

    def inputs(self, ctx: Any) -> List[str]:
        return ["db: user_beliefs (active, domain-filtered)"]

    def outputs(self, ctx: Any) -> List[str]:
        return []

    def run(self, ctx: Any, *, dry_run: bool = False) -> Dict[str, Any]:
        store = BeliefStore()
        initial = store.list_by_domain(self.domain)
        initial_count = len(initial)

        if initial_count < 2:
            ctx.canonicalization_result = {
                "status": "skipped", "reason": "too_few_beliefs", "domain": self.domain,
                "initial_belief_count": initial_count, "final_belief_count": initial_count,
                "total_merges": 0, "passes": 0,
            }
            return ctx.canonicalization_result

        mode = getattr(ctx, "canonicalization_mode", None) or decide_mode()
        last_sweep = read_last_full_sweep_at()

        focus_keys: Optional[Set[str]] = None
        if mode == "new_only":
            new_beliefs = _filter_new_beliefs_since(initial, last_sweep)
            if not new_beliefs:
                ctx.canonicalization_result = {
                    "status": "skipped", "reason": "new_only_no_new", "mode": "new_only",
                    "domain": self.domain, "initial_belief_count": initial_count,
                    "final_belief_count": initial_count, "new_belief_count": 0,
                    "total_merges": 0, "passes": 0,
                }
                return ctx.canonicalization_result
            focus_keys = {b.belief_key for b in new_beliefs}

        if dry_run:
            clusters = _find_duplicate_clusters(store, self.domain)
            ctx.canonicalization_result = {
                "status": "dry_run", "mode": mode, "domain": self.domain,
                "belief_count": initial_count,
                "candidate_clusters": [[b.belief_key for b in c] for c in clusters],
            }
            return ctx.canonicalization_result

        agent_factory = ServiceLocator.get("agent_factory")
        if agent_factory is None:
            raise RuntimeError("agent_factory not available in DI")

        logger.info("[CanonicalizeBeliefSet] domain=%s mode=%s beliefs=%d focus=%s",
                    self.domain, mode, initial_count,
                    len(focus_keys) if focus_keys is not None else "all")

        merges = _run_verifier_dedup_pass(
            initial, self.domain, store, agent_factory,
            scope_context=ctx.scope_context, focus_keys=focus_keys,
        )

        final_count = len(store.list_by_domain(self.domain))
        logger.info("[CanonicalizeBeliefSet] domain=%s done: %d -> %d (merges=%d, mode=%s)",
                    self.domain, initial_count, final_count, merges, mode)
        ctx.canonicalization_result = {
            "status": "ok", "mode": mode, "domain": self.domain,
            "initial_belief_count": initial_count, "final_belief_count": final_count,
            "total_merges": merges, "passes": 1,
        }
        return ctx.canonicalization_result
