"""
Step 4: Canonicalize near-duplicate beliefs within a domain.

Strategy:
- Load all active beliefs for the domain.
- Sort them by embedding proximity (nearest-neighbour walk) so semantically
  similar beliefs end up adjacent in the list.
- Send chunks of ~CHUNK_SIZE beliefs to the belief_canonicalizer LLM.
- The LLM decides which beliefs to merge, deprecate, or leave as-is.
- Repeat until a full pass produces zero merges (convergence).
- Max passes is capped to avoid infinite loops.

The threshold is only used to sort/group candidates. The LLM makes all
merge decisions — no hard cosine cutoff for actual merges.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

from belief_engine.store.belief_store import BeliefRecord, BeliefStore

logger = logging.getLogger(__name__)

_AGENT_NAME = "belief_engine::belief_canonicalizer"
CHUNK_SIZE = 40
MAX_PASSES = 5


def _proximity_sort(
    beliefs: List[BeliefRecord],
    store: BeliefStore,
) -> List[BeliefRecord]:
    """
    Sort beliefs so semantically similar ones are adjacent using a
    greedy nearest-neighbour walk over Chroma embeddings.

    Falls back to original order if embeddings are unavailable.
    """
    if len(beliefs) <= 1:
        return beliefs

    try:
        chroma = store._chroma
        id_to_vec: Dict[str, List[float]] = {
            bid: vec
            for bid, vec in chroma.get_all_for_domain(beliefs[0].domain)
        }
    except Exception as exc:
        logger.debug("[CanonicalizeBeliefSet] proximity sort unavailable: %s — using original order", exc)
        return beliefs

    id_to_belief = {b.id: b for b in beliefs}
    remaining: Set[str] = set(id_to_belief)

    # Start with the first belief (arbitrary seed).
    ordered: List[BeliefRecord] = []
    current_id = beliefs[0].id
    remaining.discard(current_id)
    ordered.append(id_to_belief[current_id])

    import numpy as np

    while remaining:
        current_vec = id_to_vec.get(current_id)
        if current_vec is None:
            # No embedding — just append remaining in original order.
            for b in beliefs:
                if b.id in remaining:
                    ordered.append(b)
                    remaining.discard(b.id)
            break

        cv = np.array(current_vec)
        cv_norm = np.linalg.norm(cv)

        best_id: Optional[str] = None
        best_score = -1.0

        for rid in remaining:
            rv = id_to_vec.get(rid)
            if rv is None:
                continue
            rv_arr = np.array(rv)
            rv_norm = np.linalg.norm(rv_arr)
            if cv_norm == 0 or rv_norm == 0:
                continue
            score = float(np.dot(cv, rv_arr) / (cv_norm * rv_norm))
            if score > best_score:
                best_score = score
                best_id = rid

        if best_id is None:
            # Fallback: append remaining in original order.
            for b in beliefs:
                if b.id in remaining:
                    ordered.append(b)
                    remaining.discard(b.id)
            break

        ordered.append(id_to_belief[best_id])
        remaining.discard(best_id)
        current_id = best_id

    return ordered


def _format_chunk_block(chunk: List[BeliefRecord], offset: int) -> str:
    lines = []
    for i, b in enumerate(chunk, offset + 1):
        lines.append(
            f"{i}. key={b.belief_key!r}  confidence={b.confidence}  "
            f"obs={b.observation_count}  scope={b.scope}"
        )
        lines.append(f"   {b.statement}")
        lines.append("")
    return "\n".join(lines)


def _run_canonicalization_pass(
    beliefs: List[BeliefRecord],
    domain: str,
    store: BeliefStore,
    agent_factory: Any,
) -> int:
    """
    Send all beliefs in CHUNK_SIZE chunks to the canonicalizer LLM.
    Returns total number of merges performed this pass.
    """
    total_merges = 0

    scope_context = build_system_scope_context(
        owner_id="belief_engine",
        actor_id=f"canonicalize_beliefs_{domain}",
        surface="pipeline",
        scope_id=f"scope::belief_engine::canonicalize::{domain}",
    )

    for chunk_start in range(0, len(beliefs), CHUNK_SIZE):
        chunk = beliefs[chunk_start: chunk_start + CHUNK_SIZE]
        # Skip chunks that are all already deprecated mid-pass.
        active_chunk = [
            b for b in chunk
            if store.get_by_key(b.belief_key) is not None
            and (store.get_by_key(b.belief_key) or b).status != "deprecated"
        ]
        if len(active_chunk) < 2:
            continue

        clusters_block = _format_chunk_block(active_chunk, chunk_start)

        agent = agent_factory.create_agent(_AGENT_NAME)
        if agent is None:
            raise RuntimeError(f"Agent '{_AGENT_NAME}' not found")

        msg = Message(
            agent_input={
                "task": f"Review {len(active_chunk)} beliefs from the '{domain}' domain. Merge, split, or keep as-is per your instructions.",
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
            logger.info("[CanonicalizeBeliefSet] chunk %d notes: %s", chunk_start, notes)

        for cb in canonical_beliefs:
            try:
                surviving_key = cb.get("belief_key", "")
                deprecated_keys = cb.get("deprecated_keys") or []
                if not surviving_key or not deprecated_keys:
                    continue

                store.merge_belief(
                    surviving_key=surviving_key,
                    surviving_statement=cb.get("statement", ""),
                    surviving_confidence=cb.get("confidence", "medium"),
                    surviving_scope=cb.get("scope", "chronic"),
                    deprecated_keys=deprecated_keys,
                    domain=domain,
                    merge_reasoning=cb.get("merge_reasoning", ""),
                )
                total_merges += 1
                logger.info(
                    "[CanonicalizeBeliefSet] merged %s ← %s",
                    surviving_key, deprecated_keys,
                )
            except Exception as exc:
                logger.exception(
                    "[CanonicalizeBeliefSet] merge failed surviving=%s: %s",
                    cb.get("belief_key", "?"), exc,
                )

    return total_merges


class CanonicalizeBeliefSetStep:
    """
    Step 4 of BeliefEnginePipeline.

    Repeatedly presents all active domain beliefs (sorted by proximity,
    chunked to CHUNK_SIZE) to the belief_canonicalizer LLM until no more
    merges are produced (convergence) or MAX_PASSES is reached.
    """

    name = "canonicalize_belief_set"

    def __init__(self, domain: str) -> None:
        self.domain = domain

    def inputs(self, ctx: Any) -> list:
        return ["db: user_beliefs (active, domain-filtered)"]

    def outputs(self, ctx: Any) -> list:
        return []

    def run(self, ctx: Any, *, dry_run: bool = False) -> dict:
        store = BeliefStore()
        beliefs = store.list_by_domain(self.domain)

        if len(beliefs) < 2:
            logger.info(
                "[CanonicalizeBeliefSet] domain=%s — fewer than 2 beliefs, nothing to canonicalize",
                self.domain,
            )
            ctx.canonicalization_result = {
                "status": "skipped",
                "reason": "too_few_beliefs",
                "domain": self.domain,
            }
            return ctx.canonicalization_result

        logger.info(
            "[CanonicalizeBeliefSet] domain=%s starting with %d beliefs, chunk_size=%d max_passes=%d",
            self.domain, len(beliefs), CHUNK_SIZE, MAX_PASSES,
        )

        # Sort by proximity so similar beliefs land in the same chunk.
        sorted_beliefs = _proximity_sort(beliefs, store)

        if dry_run:
            ctx.canonicalization_result = {
                "status": "dry_run",
                "domain": self.domain,
                "belief_count": len(sorted_beliefs),
                "chunks": [
                    [b.belief_key for b in sorted_beliefs[i: i + CHUNK_SIZE]]
                    for i in range(0, len(sorted_beliefs), CHUNK_SIZE)
                ],
            }
            return ctx.canonicalization_result

        agent_factory = ServiceLocator.get("agent_factory")
        if agent_factory is None:
            raise RuntimeError("agent_factory not available in DI")

        total_merged = 0
        total_deprecated = 0

        for pass_num in range(1, MAX_PASSES + 1):
            # Reload after each pass — some beliefs may have been deprecated.
            beliefs = store.list_by_domain(self.domain)
            if len(beliefs) < 2:
                logger.info("[CanonicalizeBeliefSet] domain=%s pass=%d — down to %d beliefs, done",
                            self.domain, pass_num, len(beliefs))
                break

            sorted_beliefs = _proximity_sort(beliefs, store)
            logger.info(
                "[CanonicalizeBeliefSet] domain=%s pass=%d beliefs=%d",
                self.domain, pass_num, len(beliefs),
            )

            merges_this_pass = _run_canonicalization_pass(
                sorted_beliefs, self.domain, store, agent_factory
            )
            total_merged += merges_this_pass
            # Each merge depreciates len(deprecated_keys) beliefs — track roughly.
            total_deprecated += merges_this_pass  # at minimum 1 deprecated per merge

            logger.info(
                "[CanonicalizeBeliefSet] domain=%s pass=%d merges=%d",
                self.domain, pass_num, merges_this_pass,
            )

            if merges_this_pass == 0:
                logger.info(
                    "[CanonicalizeBeliefSet] domain=%s converged after %d pass(es)",
                    self.domain, pass_num,
                )
                break
        else:
            logger.warning(
                "[CanonicalizeBeliefSet] domain=%s hit MAX_PASSES=%d without converging",
                self.domain, MAX_PASSES,
            )

        final_count = len(store.list_by_domain(self.domain))
        logger.info(
            "[CanonicalizeBeliefSet] domain=%s done: %d→%d beliefs, total_merges=%d",
            self.domain, len(beliefs), final_count, total_merged,
        )

        ctx.canonicalization_result = {
            "status": "ok",
            "domain": self.domain,
            "initial_belief_count": len(beliefs),
            "final_belief_count": final_count,
            "total_merges": total_merged,
            "passes": pass_num,
        }
        return ctx.canonicalization_result
