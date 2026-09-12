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

Cadence (belief_engine.state.sweep_tracker.decide_mode, stamped under "global"):
  - "full" (weekly): propose pairs across ALL active beliefs, every domain together.
  - "new_only" (the other nights): SKIPPED here — a new belief is verified against its nearest
    neighbours the moment it is written (UpdateBeliefsStep.dedup_candidate), so the nightly
    incremental pass has nothing left to ask. `focus_keys` remains for callers/tests that want
    a focused pass.

Durability (2026-07-07): every "not the same" verdict is recorded in belief_distinct_pairs,
bound to the statements it judged, and skipped on later passes while both statements are
unchanged. Verifier calls per run are capped, and the domain's full-sweep stamp is written
only by an un-truncated full pass — so an interrupted or capped sweep RESUMES where it left
off (paying only new pairs) instead of re-paying every prior verdict from scratch.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger
import re
from typing import Any, Dict, List, Optional, Set

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.utils.pydantic_classes import Message

from belief_engine.state.sweep_tracker import (
    decide_mode,
    mark_full_sweep_completed,
)
from belief_engine.store import distinct_pairs as verdicts
from belief_engine.store.belief_store import BeliefRecord, BeliefStore

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

logger = get_logger(__name__)

_AGENT_NAME = "belief_engine::merge_verifier"

# The relations the verifier may return. Two beliefs that are not duplicates can still
# stand in a relation the store must act on; a boolean could only ever say "different",
# which is where conflicts used to disappear.
#   same        -> merge
#   different   -> record the verdict, both stand
#   specialises -> record the verdict, both stand (one qualifies the other)
#   supersedes  -> deprecate the outdated side
#   contradicts -> mark both contested, for the reevaluator's full-trail ruling
_RELATIONS = ("same", "different", "specialises", "supersedes", "contradicts")
# Relations that leave both beliefs in place; the verdict is remembered so the pair is
# not re-paid for while nothing about either side has changed.
_INERT_RELATIONS = ("different", "specialises")

# Embedding cosine at/above which a PAIR is proposed to the verifier. Recall-biased on
# purpose — the verifier is the precision gate, so propose generously and let it reject.
MERGE_THRESHOLD = 0.80
# Backstop on candidate pairs per pass (strongest pairs first). High enough not to bite the real
# candidate count (a global sweep is ~1-2k pairs); only a pathological explosion would hit it.
MAX_PAIRS = 4000
# Bound on VERIFIER LLM CALLS actually made per run (recorded-distinct skips are free). Keeps one
# night's spend/runtime bounded; with the verdict memory the sweep converges across nights, and a
# capped (truncated) full pass leaves the domain's sweep stamp unwritten so the next run resumes.
MAX_VERIFIER_CALLS_PER_RUN = 400

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
    """(items, normalized matrix) for the beliefs that have an embedding + a statement.
    The global pass spans every domain the beliefs come from."""
    if np is None:
        raise RuntimeError("canonicalize requires numpy")
    id_to_vec: Dict[str, Any] = {}
    for dom in sorted({b.domain for b in beliefs}):
        id_to_vec.update(dict(store._chroma.get_all_for_domain(dom)))
    # Exclude owner-locked beliefs: a manual correction must never be merged away (as loser)
    # or have its statement rewritten to a canonical (as survivor).
    items = [b for b in beliefs
             if b.id in id_to_vec and (b.statement or "").strip() and not getattr(b, "locked", 0)]
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


# Recent evidence lines shown per side. Enough to date the claim and show what supported
# it; not the whole trail, which the reevaluator reads when a pair turns out to conflict.
_CONTEXT_EVIDENCE_LINES = 6


def belief_context_block(store, belief: BeliefRecord) -> str:
    """The dated history the verifier needs to tell supersedes from contradicts.

    Those two relations are the SAME two sentences — only the evidence dates separate a
    preference that changed from a genuine conflict. Judged on statements alone the
    distinction is unavailable, so the verifier could never make it and every conflict was
    filed as 'different' and never revisited.
    """
    lines = [
        f"  area: {belief.domain} | kind: {belief.kind or 'unclassified'}",
        f"  observed {belief.observation_count}x | first {belief.first_observed or '?'} "
        f"| last confirmed {belief.last_confirmed or '?'}",
    ]
    try:
        evidence = store.get_evidence(belief.id) or []
    except Exception as e:
        logger.warning("[CanonicalizeBeliefSet] evidence unavailable for %s: %s", belief.id, e)
        evidence = []
    if evidence:
        evidence = sorted(evidence, key=lambda e: (e.source_date or e.created_at or ""))
        recent = evidence[-_CONTEXT_EVIDENCE_LINES:]
        lines.append(f"  evidence ({len(evidence)} total, most recent {len(recent)}):")
        for ev in recent:
            when = ev.source_date or (ev.created_at or "")[:10]
            lines.append(f"    [{when}][{ev.signal_type}] {(ev.summary or '')[:160]}")
    else:
        lines.append("  evidence: (none recorded)")
    return "\n".join(lines)


_NEW_STATEMENT_CONTEXT = (
    "  area: (not yet filed) | kind: (not yet classified)\n"
    "  brand-new statement, proposed now — no stored history; treat its date as now"
)


def verify_relation(
    agent,
    a: BeliefRecord,
    b: BeliefRecord,
    *,
    scope_context,
    context_a: Optional[str] = None,
    context_b: Optional[str] = None,
) -> Dict[str, Any]:
    """Ask the merge_verifier how two beliefs relate.

    Returns its structured output: {relation, reason, canonical_statement, current_side}
    where relation is one of same | different | specialises | supersedes | contradicts.
    """
    resp = agent.action_handler(Message(
        agent_input={
            "phrase_a": a.statement,
            "phrase_b": b.statement,
            "context_a": context_a or "",
            "context_b": context_b or "",
            "ctype": "belief",
            "ctype_noun": "concept",
        },
        scope_context=scope_context,
    ))
    data = resp.data if resp and hasattr(resp, "data") else {}
    if not isinstance(data, dict):
        data = {
            "relation": getattr(data, "relation", None),
            "reason": getattr(data, "reason", ""),
            "canonical_statement": getattr(data, "canonical_statement", ""),
            "current_side": getattr(data, "current_side", ""),
        }
    relation = str(data.get("relation") or "").strip().lower()
    if relation not in _RELATIONS:
        # An unreadable verdict must not be guessed into a merge or a deprecation.
        # "different" is the only inert outcome, so an unusable answer lands there loudly.
        logger.error(
            "[CanonicalizeBeliefSet] verifier returned unusable relation=%r for %s / %s "
            "— treating as 'different' (no belief is changed)",
            data.get("relation"), a.belief_key, b.belief_key,
        )
        relation = "different"
    data["relation"] = relation
    return data


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
    None = propose all pairs (full sweep).

    Returns {merges, verifier_calls, skipped_distinct, truncated}: `skipped_distinct` pairs were
    settled by a recorded verdict (no LLM), and `truncated` means the per-run verifier-call cap
    stopped the pass early (the caller must NOT stamp the sweep complete).
    """
    result = {"merges": 0, "verifier_calls": 0, "skipped_distinct": 0, "truncated": False,
              "different": 0, "specialises": 0, "superseded": 0, "contradictions": 0}
    items, mat = _embeddings_for(store, beliefs)
    if not items:
        return result
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
        return result

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

    # Durable verdict memory. One dormant connection for the pass; each verdict commits its own
    # short transaction the moment it's rendered (surviving interruption is the point), so no
    # transaction is ever open across a verifier LLM call.
    verdict_conn = verdicts.open_conn()
    try:
        distinct_map = verdicts.load_distinct_map(verdict_conn)
        for _s, i, j in pairs:
            ka, kb = root(items[i].belief_key), root(items[j].belief_key)
            if ka == kb:
                continue
            ra, rb = store.get_by_key(ka), store.get_by_key(kb)
            if ra is None or rb is None or ra.status != "active" or rb.status != "active":
                continue

            # A recorded "not the same" verdict settles the pair while both statements are
            # unchanged — free skip, no LLM.
            if verdicts.is_recorded_distinct(distinct_map, ra.id, ra.statement, rb.id, rb.statement):
                result["skipped_distinct"] += 1
                continue

            if result["verifier_calls"] >= MAX_VERIFIER_CALLS_PER_RUN:
                result["truncated"] = True
                logger.info(
                    "[CanonicalizeBeliefSet] domain=%s hit the %d-call cap — pass truncated; "
                    "the next run resumes from the recorded verdicts.",
                    domain, MAX_VERIFIER_CALLS_PER_RUN)
                break

            verdict = verify_relation(
                agent, ra, rb, scope_context=scope_context,
                context_a=belief_context_block(store, ra),
                context_b=belief_context_block(store, rb),
            )
            result["verifier_calls"] += 1
            relation = verdict["relation"]
            reason = str(verdict.get("reason") or "")

            if relation in _INERT_RELATIONS:
                # Both stand. Remember it so the pair costs nothing while neither side moves.
                verdicts.record_distinct(verdict_conn, ra.id, ra.statement, rb.id, rb.statement,
                                         reason=f"{relation}: {reason}")
                result[relation] = result.get(relation, 0) + 1
                continue

            if relation == "supersedes":
                # One is a later state of the other: the old claim WAS true and no longer is.
                # Deprecating the outdated side is what the updater already does for a flip it
                # sees in evidence; the sweep could not express it before, so a changed
                # preference sat in the store forever beside its own replacement.
                side = str(verdict.get("current_side") or "").strip().lower()
                if side not in ("a", "b"):
                    logger.error(
                        "[CanonicalizeBeliefSet] %s: supersedes without a usable current_side "
                        "(%r) for %s / %s — leaving both active",
                        domain, verdict.get("current_side"), ra.belief_key, rb.belief_key)
                    result["superseded_unresolved"] = result.get("superseded_unresolved", 0) + 1
                    continue
                current, outdated = (ra, rb) if side == "a" else (rb, ra)
                try:
                    store.deprecate(outdated.belief_key,
                                    reason=f"superseded by {current.belief_key}: {reason[:200]}")
                    result["superseded"] = result.get("superseded", 0) + 1
                    logger.info("[CanonicalizeBeliefSet] %s: %s superseded by %s",
                                domain, outdated.belief_key, current.belief_key)
                except Exception:
                    logger.exception("[CanonicalizeBeliefSet] %s: deprecate failed for %s",
                                     domain, outdated.belief_key)
                continue

            if relation == "contradicts":
                # Same subject, opposing claims, and the evidence does not say which is current.
                # Neither is trustworthy, and neither should be silently dropped — hand BOTH to
                # the reevaluator, which reads each full trail and rules. Not recorded as a
                # settled verdict: this pair is unresolved, not decided.
                for side in (ra, rb):
                    try:
                        store.mark_contested(side.belief_key)
                    except Exception:
                        logger.exception("[CanonicalizeBeliefSet] %s: mark_contested failed for %s",
                                         domain, side.belief_key)
                result["contradictions"] = result.get("contradictions", 0) + 1
                logger.warning(
                    "[CanonicalizeBeliefSet] %s: CONTRADICTION between %s and %s — both contested. %s",
                    domain, ra.belief_key, rb.belief_key, reason[:200])
                continue

            # relation == "same" — Survivor = the better-supported belief (keeps the more-observed key + its history).
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
                    # The survivor keeps its own area — in the global pass the two sides
                    # may come from different domains, and "global" is not a domain.
                    domain=survivor.domain,
                    merge_reasoning=(verdict.get("reason") or "")[:300],
                )
                merged_into[loser.belief_key] = survivor.belief_key
                result["merges"] += 1
                logger.info("[CanonicalizeBeliefSet] domain=%s merged %s <- %s",
                            domain, survivor.belief_key, loser.belief_key)
            except Exception:
                logger.exception("[CanonicalizeBeliefSet] domain=%s merge failed surviving=%s",
                                 domain, survivor.belief_key)
    finally:
        verdict_conn.close()

    return result


class CanonicalizeBeliefSetStep:
    """Step 5 of BeliefEnginePipeline — pairwise-verifier dedup (see module docstring).

    Global by default (domain=None): the pass spans every active belief, so a duplicate filed
    under another area is a candidate. New beliefs are deduplicated at WRITE time by
    UpdateBeliefsStep, so the incremental "new_only" night is skipped here; this step runs
    the weekly full sweep only (drift, reevaluation rewrites, anything write-time dedup
    could not see).
    """

    name = "canonicalize_belief_set"

    def __init__(self, domain: Optional[str] = None) -> None:
        self.domain = domain

    @property
    def _label(self) -> str:
        return self.domain or "global"

    def _active(self, store: BeliefStore) -> List[BeliefRecord]:
        return store.list_all(status="active") if self.domain is None else store.list_by_domain(self.domain)

    def inputs(self, ctx: Any) -> List[str]:
        return ["db: user_beliefs (active)"]

    def outputs(self, ctx: Any) -> List[str]:
        return []

    def run(self, ctx: Any, *, dry_run: bool = False) -> Dict[str, Any]:
        store = BeliefStore()
        initial = self._active(store)
        initial_count = len(initial)
        label = self._label

        mode = getattr(ctx, "canonicalization_mode", None) or decide_mode(domain=label)

        if initial_count < 2:
            # A set too small to pair is a trivially-complete full sweep — stamp it so it
            # doesn't stay in weekly-full mode forever.
            if mode == "full" and not dry_run:
                mark_full_sweep_completed(label)
            ctx.canonicalization_result = {
                "status": "skipped", "reason": "too_few_beliefs", "mode": mode,
                "domain": label,
                "initial_belief_count": initial_count, "final_belief_count": initial_count,
                "total_merges": 0, "passes": 0,
            }
            return ctx.canonicalization_result

        if mode == "new_only":
            # New beliefs were verified against their nearest neighbours when they were
            # written (UpdateBeliefsStep); re-proposing every pair they touch would pay for
            # the same question again. The weekly full sweep catches the rest.
            ctx.canonicalization_result = {
                "status": "skipped", "reason": "new_only_handled_at_write_time", "mode": "new_only",
                "domain": label, "initial_belief_count": initial_count,
                "final_belief_count": initial_count, "total_merges": 0, "passes": 0,
            }
            return ctx.canonicalization_result

        if dry_run:
            clusters = _find_duplicate_clusters(store, self.domain) if self.domain else []
            ctx.canonicalization_result = {
                "status": "dry_run", "mode": mode, "domain": label,
                "belief_count": initial_count,
                "candidate_clusters": [[b.belief_key for b in c] for c in clusters],
            }
            return ctx.canonicalization_result

        agent_factory = ServiceLocator.get("agent_factory")
        if agent_factory is None:
            raise RuntimeError("agent_factory not available in DI")

        logger.info("[CanonicalizeBeliefSet] %s mode=%s beliefs=%d", label, mode, initial_count)

        pass_result = _run_verifier_dedup_pass(
            initial, label, store, agent_factory,
            scope_context=ctx.scope_context, focus_keys=None,
        )

        # The full-sweep stamp is written HERE, by an un-truncated full pass — a capped pass
        # stays "full" next run and resumes from the recorded verdicts.
        if not pass_result["truncated"]:
            mark_full_sweep_completed(label)

        final_count = len(self._active(store))
        logger.info(
            "[CanonicalizeBeliefSet] %s done: %d -> %d (merges=%d superseded=%d "
            "contradictions=%d specialises=%d different=%d calls=%d skipped_distinct=%d "
            "truncated=%s mode=%s)",
            label, initial_count, final_count, pass_result["merges"],
            pass_result.get("superseded", 0), pass_result.get("contradictions", 0),
            pass_result.get("specialises", 0), pass_result.get("different", 0),
            pass_result["verifier_calls"], pass_result["skipped_distinct"],
            pass_result["truncated"], mode)
        ctx.canonicalization_result = {
            "status": "ok", "mode": mode, "domain": label,
            "initial_belief_count": initial_count, "final_belief_count": final_count,
            "total_merges": pass_result["merges"], "passes": 1,
            "superseded": pass_result.get("superseded", 0),
            "contradictions": pass_result.get("contradictions", 0),
            "specialises": pass_result.get("specialises", 0),
            "different": pass_result.get("different", 0),
            "verifier_calls": pass_result["verifier_calls"],
            "skipped_distinct": pass_result["skipped_distinct"],
            "truncated": pass_result["truncated"],
        }
        return ctx.canonicalization_result
