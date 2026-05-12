"""
Step 4: Canonicalize near-duplicate beliefs within a domain.

Strategy:
- Load all active beliefs for the domain.
- Sort them by embedding proximity using a nearest-neighbour walk so
  semantically similar beliefs tend to be adjacent.
- Send overlapping chunks of beliefs to the belief_canonicalizer LLM.
- The LLM decides which beliefs to merge or leave as-is.
- Repeat until a full pass produces zero merges.
- Max passes is capped to avoid infinite loops.

The embedding proximity is only used to sort/group candidates. The LLM makes
all merge decisions. There is no hard cosine cutoff for actual merges.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

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
) -> int:
    """
    Send all beliefs in overlapping chunks to the canonicalizer LLM.

    Returns the total number of merge operations performed this pass.
    """
    total_merges = 0

    scope_context = build_system_scope_context(
        owner_id="belief_engine",
        actor_id=f"canonicalize_beliefs_{domain}",
        surface="pipeline",
        scope_id=f"scope::belief_engine::canonicalize::{domain}",
    )

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


class CanonicalizeBeliefSetStep:
    """
    Step 4 of BeliefEnginePipeline.

    Repeatedly presents all active domain beliefs, sorted by proximity and
    grouped into overlapping chunks, to the belief_canonicalizer LLM until no
    more merges are produced or MAX_PASSES is reached.
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

        logger.info(
            "[CanonicalizeBeliefSet] domain=%s starting with %d beliefs, chunk_size=%d overlap=%d max_passes=%d",
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
            "domain": self.domain,
            "initial_belief_count": initial_count,
            "final_belief_count": final_count,
            "total_merges": total_merges,
            "passes": passes_run,
            "converged": converged,
        }
        return ctx.canonicalization_result