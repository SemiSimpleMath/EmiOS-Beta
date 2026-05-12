"""
context_activation.py — KG activation + situation brief generation.

Stage 2 of the context_engine pipeline.

Steps:
    1. Resolve seed labels to KG node IDs (resolve_entity_to_node_id).
    2. Run multi_seed_activation to find convergence nodes + second-wave expansions.
    3. Build a compact plain-text KG briefing from the activation result.
    4. Call Gemini once (no schema) to synthesise a structured markdown dossier.

Output: ActivationResult
    - kg_briefing:   raw KG convergence text (for debugging)
    - situation_brief: the LLM-synthesised markdown dossier
    - seeds_resolved: dict[label -> node_id]
    - elapsed_seconds: float
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from pydantic import BaseModel

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Tuning defaults (can be overridden per-call)
# ---------------------------------------------------------------------------

DEFAULT_DEPTH = 2
DEFAULT_TOP_K_CONVERGENCE = 15
DEFAULT_IMPORTANCE_THRESHOLD = 0.5
DEFAULT_SECOND_WAVE_IMPORTANCE = 0.4
DEFAULT_SECOND_WAVE_MAX = 12


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

class ActivationResult(BaseModel):
    kg_briefing: str
    situation_brief: str
    seeds_resolved: Dict[str, Optional[str]]  # label → node_id (None if unresolved)
    elapsed_seconds: float


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_kg_briefing(
    *,
    result,
    user_message: str,
    seeds: List[str],
    importance_threshold: float,
    second_wave_importance: float,
    second_wave_max: int,
) -> str:
    seed_label = " + ".join(seeds)
    lines = [
        f"## KG Convergence Briefing",
        f'User said: "{user_message}"',
        f"Seeds: {seed_label}",
        "",
        "### Convergence Nodes — shared between all seeds",
    ]

    kept = [
        n for n in result.convergence_nodes
        if n.importance >= importance_threshold or n.node_type in ("Entity", "Concept")
    ]
    if not kept:
        lines.append("(none above threshold)")
    for n in kept:
        desc = f" — {n.description}" if n.description else ""
        lines.append(
            f"- [{n.node_type}] **{n.label}** "
            f"(importance={n.importance:.2f}, score={n.convergence_score:.3f}){desc}"
        )

    lines += ["", "### Second-Wave Expansions — 1 hop beyond convergence zone"]
    wave2 = [x for x in result.second_wave_nodes[:second_wave_max] if x.importance >= second_wave_importance]
    if not wave2:
        lines.append("(none above threshold)")
    for n in wave2:
        desc = f" — {n.description}" if n.description else ""
        lines.append(
            f"- [{n.node_type}] **{n.label}** (importance={n.importance:.2f}){desc}"
        )

    return "\n".join(lines)


def _synthesise_brief(
    *,
    kg_briefing: str,
    user_message: str,
    seeds: List[str],
    primary_user: str,
) -> str:
    """Single Gemini call: KG briefing → markdown dossier."""
    visitor_seeds = [s for s in seeds if s.lower() != primary_user.lower()]
    visitor_label = visitor_seeds[0] if visitor_seeds else seeds[0]

    prompt = f"""\
You are a personal assistant preparing a situation dossier from a Knowledge Graph briefing.

STRICT GROUNDING RULE: Every name, fact, and claim you write must appear explicitly in the \
briefing below. Do not infer, guess, or fill in names or details from outside knowledge. \
If a person's name is not in the briefing, refer to them by their relationship label only \
(e.g. "Jukka's son", not a guessed name). If a fact is absent, say it is unknown.

The user said: "{user_message}"

Below is the Knowledge Graph convergence briefing — the most relevant nodes and connections \
linking {", ".join(seeds)}.

{kg_briefing}

Produce a concise situation dossier with these sections:

## Relationship
One sentence: how are {visitor_label} and {primary_user} connected?

## Shared History & Context
What connects them historically? Note any gaps (dates unknown, institutions unclear, etc.).

## Pending Goals & Actions
What goals, tasks, or open loops exist in the graph related to this situation?

## Missing Edges
Relationships structurally implied by the graph but NOT explicitly recorded.
For each: who, what connection is missing, and why it matters.

## Questions for {primary_user}
3-5 concrete questions {primary_user} should address — things the graph cannot answer.

## Researchable via KG
2-3 narrow questions that could plausibly be answered by querying the KG further.
"""

    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message

    agent = DI.agent_factory.create_agent("context_engine::dossier_writer")
    agent.blackboard.update_state_value("user_message", user_message)
    agent.blackboard.update_state_value("kg_briefing", kg_briefing)
    agent.blackboard.update_state_value("seeds", ", ".join(seeds))
    agent.blackboard.update_state_value("primary_user", primary_user)
    agent.blackboard.update_state_value("visitor_label", visitor_label)

    msg = Message(agent_input={
        "user_message": user_message,
        "kg_briefing": kg_briefing,
        "seeds": ", ".join(seeds),
        "primary_user": primary_user,
        "visitor_label": visitor_label,
    })
    result = agent.action_handler(msg)
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        sections = []
        for key in ("relationship", "shared_history", "pending_goals", "missing_edges", "questions_for_user", "researchable_via_kg"):
            val = str(data.get(key) or "").strip()
            if val:
                header = key.replace("_", " ").title()
                sections.append(f"## {header}\n{val}")
        return "\n\n".join(sections)

    raise RuntimeError("dossier_writer agent returned no structured data.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_context_activation(
    *,
    seeds: List[str],
    user_message: str,
    primary_user: str,
    owner_id: str,
    depth: int = DEFAULT_DEPTH,
    top_k_convergence: int = DEFAULT_TOP_K_CONVERGENCE,
    importance_threshold: float = DEFAULT_IMPORTANCE_THRESHOLD,
    second_wave_importance: float = DEFAULT_SECOND_WAVE_IMPORTANCE,
    second_wave_max: int = DEFAULT_SECOND_WAVE_MAX,
) -> ActivationResult:
    """
    Run KG activation for the given seeds and synthesise a situation brief.

    Args:
        seeds:             Entity labels to activate (e.g. ["Rahul", "Jukka"]).
        user_message:      The original user message that triggered activation.
        primary_user:      Label of the primary user node in the KG.
        owner_id:          KG owner identifier used for session scoping.
        depth:             BFS depth for multi_seed_activation.
        top_k_convergence: Max convergence nodes to surface.
        importance_threshold:      Min importance for convergence nodes to include.
        second_wave_importance:    Min importance for second-wave nodes to include.
        second_wave_max:           Max second-wave nodes to include.

    Returns:
        ActivationResult with kg_briefing, situation_brief, seed resolution map, timing.

    Raises:
        RuntimeError: if any seed label cannot be resolved to a KG node.
    """
    t0 = time.perf_counter()

    from app.models.base import get_session
    from app.assistant.kg_core.kg_utils.kg_convergence import (
        multi_seed_activation,
        resolve_entity_to_node_id,
    )

    session = get_session()
    try:
        seeds_resolved: Dict[str, Optional[str]] = {}
        seed_ids: List[str] = []

        for label in seeds:
            node_id = resolve_entity_to_node_id(session, label)
            seeds_resolved[label] = node_id
            if node_id:
                seed_ids.append(node_id)
            else:
                logger.debug("context_activation: could not resolve seed label=%r", label)

        unresolved = [lbl for lbl, nid in seeds_resolved.items() if nid is None]
        if unresolved:
            raise RuntimeError(
                f"context_activation: could not resolve seed(s): {unresolved!r}. "
                "Ensure the entities exist in the KG before activating."
            )

        logger.debug(
            "context_activation: running multi_seed_activation seeds=%r depth=%d top_k=%d",
            seeds,
            depth,
            top_k_convergence,
        )
        result = multi_seed_activation(
            session,
            seed_node_ids=seed_ids,
            depth=depth,
            top_k_convergence=top_k_convergence,
        )
    finally:
        session.close()

    kg_briefing = _build_kg_briefing(
        result=result,
        user_message=user_message,
        seeds=seeds,
        importance_threshold=importance_threshold,
        second_wave_importance=second_wave_importance,
        second_wave_max=second_wave_max,
    )

    logger.debug("context_activation: synthesising situation_brief via Gemini")
    situation_brief = _synthesise_brief(
        kg_briefing=kg_briefing,
        user_message=user_message,
        seeds=seeds,
        primary_user=primary_user,
    )

    elapsed = time.perf_counter() - t0
    logger.debug("context_activation: done in %.1fs", elapsed)

    return ActivationResult(
        kg_briefing=kg_briefing,
        situation_brief=situation_brief,
        seeds_resolved=seeds_resolved,
        elapsed_seconds=elapsed,
    )
