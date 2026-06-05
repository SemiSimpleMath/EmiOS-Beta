"""
Step 4: Canonicalize near-duplicate beliefs within a domain.

Two modes, decided per run by `belief_engine.state.sweep_tracker.decide_mode`:

  - "full" (default cadence: weekly, plus bootstrap on first run):
    Load ALL active beliefs for the domain, sort by embedding proximity,
    send overlapping chunks to the canonicalizer LLM, multi-pass until
    converged. After the last domain completes its full sweep, the
    pipeline marks the timestamp.

  - "new_only" (default cadence: 6 of 7 nights):
    Load only beliefs created since the last full sweep. Skip the
    domain entirely if fewer than 2 new beliefs (nothing to merge among
    themselves). One pass, no multi-pass loop, no overlap — chunks are
    tiny by definition.

The hot path is "new_only" — usually 0-3 LLM calls per night across all
domains. The weekly full sweep is the catch-net for cross-belief
duplicates the new-only mode skipped (existing belief drifted, or
reevaluator rewrote one belief into a near-duplicate of another).

The embedding proximity is only used to sort/group candidates. The LLM
makes all merge decisions. There is no hard cosine cutoff for actual
merges.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

from belief_engine.state.sweep_tracker import (
    decide_mode,
    read_last_full_sweep_at,
)
from belief_engine.store.belief_store import BeliefRecord, BeliefStore

logger = logging.getLogger(__name__)

_AGENT_NAME = "belief_engine::belief_canonicalizer"
CHUNK_SIZE = 40
CHUNK_OVERLAP = 8
MAX_PASSES = 5


def _proximity_sort(
        beliefs: List[BeliefRecord],
        store: BeliefStore,
) -> List[BeliefRecord]:
    """
    Sort beliefs so semantically similar ones are adjacent using a greedy
    nearest-neighbour walk over Chroma embeddings.

    Falls back to original order if embeddings are unavailable.
    """
    if len(beliefs) <= 1:
        return beliefs

    try:
        # TODO: Replace private access with a public BeliefStore method, e.g.
        # store.get_embeddings_for_domain(domain), when available.
        chroma = store._chroma
        id_to_vec: Dict[str, List[float]] = {
            belief_id: vec
            for belief_id, vec in chroma.get_all_for_domain(beliefs[0].domain)
        }
    except Exception as exc:
        logger.debug(
            "[CanonicalizeBeliefSet] proximity sort unavailable: %s; using original order",
            exc,
        )
        return beliefs

    id_to_belief = {belief.id: belief for belief in beliefs}
    remaining: Set[str] = set(id_to_belief)

    ordered: List[BeliefRecord] = []
    current_id = beliefs[0].id

    remaining.discard(current_id)
    ordered.append(id_to_belief[current_id])

    import numpy as np

    while remaining:
        current_vec = id_to_vec.get(current_id)
        if current_vec is None:
            _append_remaining_in_original_order(ordered, remaining, beliefs)
            break

        cv = np.array(current_vec)
        cv_norm = np.linalg.norm(cv)

        best_id: Optional[str] = None
        best_score = -1.0

        for remaining_id in remaining:
            remaining_vec = id_to_vec.get(remaining_id)
            if remaining_vec is None:
                continue

            rv = np.array(remaining_vec)
            rv_norm = np.linalg.norm(rv)

            if cv_norm == 0 or rv_norm == 0:
                continue

            score = float(np.dot(cv, rv) / (cv_norm * rv_norm))
            if score > best_score:
                best_score = score
                best_id = remaining_id

        if best_id is None:
            _append_remaining_in_original_order(ordered, remaining, beliefs)
            break

        ordered.append(id_to_belief[best_id])
        remaining.discard(best_id)
        current_id = best_id

    return ordered


def _append_remaining_in_original_order(
        ordered: List[BeliefRecord],
        remaining: Set[str],
        beliefs: List[BeliefRecord],
) -> None:
    """Append remaining beliefs in their original order."""
    for belief in beliefs:
        if belief.id in remaining:
            ordered.append(belief)
            remaining.discard(belief.id)


def _iter_overlapping_chunks(
        beliefs: List[BeliefRecord],
        *,
        chunk_size: int = CHUNK_SIZE,
        overlap: int = CHUNK_OVERLAP,
) -> List[List[BeliefRecord]]:
    """
    Build overlapping chunks so near-duplicates at chunk boundaries can still
    be reviewed together.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    if overlap < 0:
        raise ValueError("overlap cannot be negative")

    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    chunks: List[List[BeliefRecord]] = []
    step = chunk_size - overlap

    for start in range(0, len(beliefs), step):
        chunk = beliefs[start: start + chunk_size]
        if chunk:
            chunks.append(chunk)

        if start + chunk_size >= len(beliefs):
            break

    return chunks


def _get_active_current_chunk(
        chunk: List[BeliefRecord],
        store: BeliefStore,
) -> List[BeliefRecord]:
    """
    Refresh chunk records from the store and drop beliefs that were deprecated
    earlier in the same pass.
    """
    active_chunk: List[BeliefRecord] = []

    for belief in chunk:
        current = store.get_by_key(belief.belief_key)
        if current is None:
            continue
        if current.status == "deprecated":
            continue
        active_chunk.append(current)

    return active_chunk


def _format_chunk_block(chunk: List[BeliefRecord]) -> str:
    lines: List[str] = []

    for i, belief in enumerate(chunk, 1):
        lines.append(
            f"{i}. key={belief.belief_key!r}  "
            f"confidence={belief.confidence}  "
            f"obs={belief.observation_count}  "
            f"scope={belief.scope}"
        )
        lines.append(f"   {belief.statement}")
        lines.append("")

    return "\n".join(lines)


def _run_canonicalization_pass(
        beliefs: List[BeliefRecord],
        domain: str,
        store: BeliefStore,
        agent_factory: Any,
        *,
        scope_context: Any,
) -> int:
    """
    Send all beliefs in overlapping chunks to the canonicalizer LLM.

    Returns the total number of merge operations performed this pass.

    ``scope_context`` is built once by the pipeline and threaded in via the
    CanonicalizeBeliefSetStep — this pass receives it, does not build its own.
    """
    total_merges = 0

    chunks = _iter_overlapping_chunks(beliefs)

    for chunk_index, chunk in enumerate(chunks, 1):
        active_chunk = _get_active_current_chunk(chunk, store)

        if len(active_chunk) < 2:
            continue

        clusters_block = _format_chunk_block(active_chunk)

        agent = agent_factory.create_agent(_AGENT_NAME)
        if agent is None:
            raise RuntimeError(f"Agent '{_AGENT_NAME}' not found")

        msg = Message(
            agent_input={
                "task": (
                    f"Review {len(active_chunk)} beliefs from the '{domain}' domain. "
                    "Merge duplicate beliefs or keep them as-is per your instructions."
                ),
                "clusters_block": clusters_block,
                "domain": domain,
                "date_today": get_local_time_str(),
            },
            scope_context=scope_context,
        )

        resp = agent.action_handler(msg)
        payload = resp.data if resp and hasattr(resp, "data") else {}

        canonical_beliefs = payload.get("canonical_beliefs") or []
        notes = payload.get("canonicalization_notes")

        if notes:
            logger.info(
                "[CanonicalizeBeliefSet] domain=%s chunk=%d notes: %s",
                domain,
                chunk_index,
                notes,
            )

        for canonical_belief in canonical_beliefs:
            surviving_key = canonical_belief.get("belief_key", "")
            deprecated_keys = canonical_belief.get("deprecated_keys") or []

            if not surviving_key or not deprecated_keys:
                continue

            try:
                store.merge_belief(
                    surviving_key=surviving_key,
                    surviving_statement=canonical_belief.get("statement", ""),
                    surviving_confidence=canonical_belief.get("confidence", "medium"),
                    surviving_scope=canonical_belief.get("scope", "chronic"),
                    deprecated_keys=deprecated_keys,
                    domain=domain,
                    merge_reasoning=canonical_belief.get("merge_reasoning", ""),
                )

                total_merges += 1

                logger.info(
                    "[CanonicalizeBeliefSet] domain=%s merged %s <- %s",
                    domain,
                    surviving_key,
                    deprecated_keys,
                )

            except Exception as exc:
                logger.exception(
                    "[CanonicalizeBeliefSet] domain=%s merge failed surviving=%s: %s",
                    domain,
                    surviving_key,
                    exc,
                )

    return total_merges


def _filter_new_beliefs_since(
    beliefs: List[BeliefRecord],
    cutoff_utc: Optional[datetime],
) -> List[BeliefRecord]:
    """Return beliefs whose `created_at` is strictly after the cutoff.

    cutoff_utc=None (no prior sweep recorded) → returns everything,
    so the first run after bootstrap canonicalizes the full set.
    Defensive on parse failure: treats unparseable timestamps as
    "new" (better to include than silently skip).
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
        # String compare is fine for ISO 8601 timestamps with a
        # consistent timezone offset. Handle trailing Z by normalizing.
        s = str(created_at).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if s > cutoff_iso:
            out.append(b)
    return out


class CanonicalizeBeliefSetStep:
    """
    Step 4 of BeliefEnginePipeline.

    Modes (decided via belief_engine.state.sweep_tracker.decide_mode):
      - "full":     traditional multi-pass over all beliefs in the domain.
      - "new_only": single pass over beliefs created since the last full
                    sweep; skip if < 2.

    The pipeline itself decides when to call `mark_full_sweep_completed()`
    after the last domain completes a full sweep — this step doesn't
    write the state itself (because it runs per-domain).
    """

    name = "canonicalize_belief_set"

    def __init__(self, domain: str) -> None:
        self.domain = domain

    def inputs(self, ctx: Any) -> List[str]:
        return ["db: user_beliefs (active, domain-filtered)"]

    def outputs(self, ctx: Any) -> List[str]:
        return []

    def run(self, ctx: Any, *, dry_run: bool = False) -> Dict[str, Any]:
        store = BeliefStore()

        initial_beliefs = store.list_by_domain(self.domain)
        initial_count = len(initial_beliefs)

        if initial_count < 2:
            logger.info(
                "[CanonicalizeBeliefSet] domain=%s fewer than 2 beliefs, nothing to canonicalize",
                self.domain,
            )
            ctx.canonicalization_result = {
                "status": "skipped",
                "reason": "too_few_beliefs",
                "domain": self.domain,
                "initial_belief_count": initial_count,
                "final_belief_count": initial_count,
                "total_merges": 0,
                "passes": 0,
            }
            return ctx.canonicalization_result

        # ---- decide mode ----
        # Honor an explicit override on ctx if the pipeline driver set
        # one for this run; otherwise consult the sweep tracker. This
        # lets the pipeline make ONE decision for all domains and pass
        # it down, avoiding skew if midnight rolls over mid-pipeline.
        mode = getattr(ctx, "canonicalization_mode", None) or decide_mode()
        last_sweep = read_last_full_sweep_at()

        if mode == "new_only":
            new_beliefs = _filter_new_beliefs_since(initial_beliefs, last_sweep)
            if len(new_beliefs) < 2:
                logger.info(
                    "[CanonicalizeBeliefSet] domain=%s new_only: %d new belief(s) since last sweep — skipping",
                    self.domain, len(new_beliefs),
                )
                ctx.canonicalization_result = {
                    "status": "skipped",
                    "reason": "new_only_too_few_new",
                    "mode": "new_only",
                    "domain": self.domain,
                    "initial_belief_count": initial_count,
                    "final_belief_count": initial_count,
                    "new_belief_count": len(new_beliefs),
                    "total_merges": 0,
                    "passes": 0,
                }
                return ctx.canonicalization_result

            sorted_new = _proximity_sort(new_beliefs, store)
            if dry_run:
                ctx.canonicalization_result = {
                    "status": "dry_run",
                    "mode": "new_only",
                    "domain": self.domain,
                    "new_belief_count": len(new_beliefs),
                    "new_belief_keys": [b.belief_key for b in sorted_new],
                }
                return ctx.canonicalization_result

            agent_factory = ServiceLocator.get("agent_factory")
            if agent_factory is None:
                raise RuntimeError("agent_factory not available in DI")

            logger.info(
                "[CanonicalizeBeliefSet] domain=%s new_only: %d new beliefs to merge among themselves",
                self.domain, len(new_beliefs),
            )
            merges = _run_canonicalization_pass(sorted_new, self.domain, store, agent_factory, scope_context=ctx.scope_context)
            final_count = len(store.list_by_domain(self.domain))
            ctx.canonicalization_result = {
                "status": "ok",
                "mode": "new_only",
                "domain": self.domain,
                "initial_belief_count": initial_count,
                "final_belief_count": final_count,
                "new_belief_count": len(new_beliefs),
                "total_merges": merges,
                "passes": 1,
            }
            return ctx.canonicalization_result

        # ---- full mode (existing behavior) ----
        logger.info(
            "[CanonicalizeBeliefSet] domain=%s full sweep: %d beliefs, chunk_size=%d overlap=%d max_passes=%d",
            self.domain,
            initial_count,
            CHUNK_SIZE,
            CHUNK_OVERLAP,
            MAX_PASSES,
        )

        sorted_beliefs = _proximity_sort(initial_beliefs, store)

        if dry_run:
            chunks = _iter_overlapping_chunks(sorted_beliefs)
            ctx.canonicalization_result = {
                "status": "dry_run",
                "mode": "full",
                "domain": self.domain,
                "belief_count": initial_count,
                "chunk_size": CHUNK_SIZE,
                "chunk_overlap": CHUNK_OVERLAP,
                "chunks": [
                    [belief.belief_key for belief in chunk]
                    for chunk in chunks
                ],
            }
            return ctx.canonicalization_result

        agent_factory = ServiceLocator.get("agent_factory")
        if agent_factory is None:
            raise RuntimeError("agent_factory not available in DI")

        total_merges = 0
        passes_run = 0
        converged = False

        for pass_num in range(1, MAX_PASSES + 1):
            passes_run = pass_num

            current_beliefs = store.list_by_domain(self.domain)
            current_count = len(current_beliefs)

            if current_count < 2:
                logger.info(
                    "[CanonicalizeBeliefSet] domain=%s pass=%d down to %d beliefs, done",
                    self.domain,
                    pass_num,
                    current_count,
                )
                converged = True
                break

            sorted_beliefs = _proximity_sort(current_beliefs, store)

            logger.info(
                "[CanonicalizeBeliefSet] domain=%s pass=%d beliefs=%d",
                self.domain,
                pass_num,
                current_count,
            )

            merges_this_pass = _run_canonicalization_pass(
                sorted_beliefs,
                self.domain,
                store,
                agent_factory,
                scope_context=ctx.scope_context,
            )

            total_merges += merges_this_pass

            logger.info(
                "[CanonicalizeBeliefSet] domain=%s pass=%d merges=%d",
                self.domain,
                pass_num,
                merges_this_pass,
            )

            if merges_this_pass == 0:
                logger.info(
                    "[CanonicalizeBeliefSet] domain=%s converged after %d pass(es)",
                    self.domain,
                    pass_num,
                )
                converged = True
                break

        if not converged:
            logger.warning(
                "[CanonicalizeBeliefSet] domain=%s hit MAX_PASSES=%d without converging",
                self.domain,
                MAX_PASSES,
            )

        final_count = len(store.list_by_domain(self.domain))

        logger.info(
            "[CanonicalizeBeliefSet] domain=%s done: %d -> %d beliefs, total_merges=%d passes=%d",
            self.domain,
            initial_count,
            final_count,
            total_merges,
            passes_run,
        )

        ctx.canonicalization_result = {
            "status": "ok",
            "mode": "full",
            "domain": self.domain,
            "initial_belief_count": initial_count,
            "final_belief_count": final_count,
            "total_merges": total_merges,
            "passes": passes_run,
            "converged": converged,
        }
        return ctx.canonicalization_result