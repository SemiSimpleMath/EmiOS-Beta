"""Consumer-side gates and thresholds — single source of truth for "is X
important enough to do Y."

Before this module, every consumer that wanted to read importance and
make a decision against a threshold had its own constant in its own
file. The thresholds drifted apart over time, and any conceptual change
to "what counts as important" required hunting through the codebase.

This module centralizes the thresholds AND provides the type-aware
gates that consumers should call instead of computing the gate inline.

## What's a "consumer"?

Anything that reads importance and produces a yes/no, a priority, or a
filtered list. Examples:

  - The card-worthiness gate (should this Entity get a card?)
  - The wiki growth selector (should this Entity get a wiki page?)
  - The state-decay priority (which States to close first?)
  - The timeline gap filter (which gaps deserve a confirmation prompt?)
  - The context-activation second-wave threshold

Each gate has slightly different semantics — they're not the same
question — but they all share the importance-read pattern. By
centralizing here, changing a threshold is a one-file edit and adding
a new gate has an obvious home.

## Thresholds vs gates

  - THRESHOLD: a numeric constant. e.g. `CARD_FACT_FLOOR = 0.6` —
    consumers compare effective importance against it.

  - GATE: a function returning a boolean. e.g.
    `is_card_worthy(node) -> bool` — consumers call it instead of
    coding the gate inline. The gate reads the relevant thresholds
    AND any structural conditions (node_type, observation_count, etc.)
    from one place.

The card-worthiness gate is the cleanest example of why gates beat
raw thresholds: it's "observation_count >= 2 OR (observation_count == 1
AND degree >= 10) OR locked_by_user_at IS NOT NULL." That's three
conditions, none of which is a simple importance threshold. Putting
the policy in a function rather than three boolean clauses scattered
through builder.py makes intent obvious.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node


# ── Thresholds (consolidated from scattered locations) ─────────────────────

# CARD_FACT_FLOOR
# ---------------
# What it gates: in entity_cards_v2/builder.py:_collect_candidate_facts,
# a fact is admitted to the renderer if its importance ≥ this OR if
# the section_tagger has labeled it as contact info.
#
# Why 0.6 specifically: the importance distribution clusters bimodally.
# True trivia (one-off States like "Katy was upstairs at 3pm") lands
# at 0.1-0.5 — typically max(src×edge/10) with src.importance ~ 2 and
# edge.importance ~ 2 → 0.4. Contact info bottoms out around 0.78-0.95
# (e.g. Phil's phone State at 0.85). Identity-defining facts start
# around 1.5 and climb. A floor of 0.6 cleanly drops the trivia
# cluster while sparing the contact-info tail. (Contact info is also
# carved out separately so the floor isn't load-bearing for it.)
#
# Why not higher (like 2 or 3): cards built for sparse entities (one-
# observation people) would empty out — most of their facts cluster
# in 1-2. The MIN_KEEP safety net catches genuinely sparse cases but
# only if the floor isn't already starving them at generation time.
CARD_FACT_FLOOR: float = 0.6

# CARD_FACT_MIN_KEEP
# ------------------
# What it gates: after the floor + contact carve-out runs, if fewer
# than this many facts survived, the next-most-important facts below
# the floor are added until count reaches MIN_KEEP. Sparse-entity
# safety net.
#
# Why 50: from observation, hub entities (Jukka, Katy) easily clear
# the floor with hundreds of facts; sparse entities (passing
# acquaintances) need 30-50 facts to produce a non-trivial card with
# proper section coverage. 50 balances "don't starve a thin card" with
# "don't pad a hub card with low-quality material."
CARD_FACT_MIN_KEEP: int = 50

# CONTEXT_ACTIVATION_THRESHOLD / CONTEXT_ACTIVATION_SECOND_WAVE_THRESHOLD
# ----------------------------------------------------------------------
# What they gate: context_engine/context_activation.py's KG convergence
# graph activation. When the engine fires on a user message, it builds
# a graph of "what's contextually relevant right now." A node enters
# wave 1 (convergence zone) if its node-type is Entity/Concept OR its
# importance clears CONTEXT_ACTIVATION_THRESHOLD. Wave 2 (one-hop
# expansion) admits at CONTEXT_ACTIVATION_SECOND_WAVE_THRESHOLD.
#
# Why deliberately low (0.5 / 0.4 on the 0-10 scale): convergence
# graph activation is the "build the briefing" step, not the "gate
# attention" step. The primary signal is `convergence_score` (how
# central the node is to the seeds the engine was given); importance
# is a noise filter that excludes only the absolute bottom tier. A
# higher threshold would risk dropping context the user is actively
# discussing just because the rater hasn't caught up yet.
#
# Conventional rule: raise these only if telemetry shows the wave-1
# briefing is consistently noisy. Lowering further is fine.
CONTEXT_ACTIVATION_THRESHOLD: float = 0.5
CONTEXT_ACTIVATION_SECOND_WAVE_THRESHOLD: float = 0.4

# TIMELINE_CONFIRMATION_FLOOR
# ---------------------------
# What it gates: kg/timeline_gaps.py. The timeline_gaps module surfaces
# `?est` (estimated date) candidates as questions the conversation
# starter might ask. Asking "I have your wedding around Sept 2003 — is
# that the exact date?" is useful; asking "I have Katy as upstairs
# around March 2026 — was that exactly when?" is noise.
#
# Why 6.0: empirically, facts that the user might want to confirm
# cluster at importance ≥ 6 (life events: marriages, births, jobs,
# moves). Below 6, facts are ambient — moments in time, fleeting states
# the user won't enjoy being quizzed on. 6 was tuned by walking the
# timeline output and asking "would I want to be asked about this?"
# Things at 4-5 mostly got "no."
#
# This is one of the few thresholds calibrated against actual judgment,
# not just structural clustering — it lives in user-attention space.
TIMELINE_CONFIRMATION_FLOOR: float = 6.0

# LENS_BLEND_IMPORTANCE_WEIGHT / LENS_BLEND_PAGERANK_WEIGHT
# ---------------------------------------------------------
# What they gate: node_importance.py's `get_important_nodes` and
# step_description_fill priority blending. When sorting nodes by "how
# important," combine importance and PageRank with this blend:
#   priority = IW * node.importance + PW * node.pagerank_score
#
# Why 0.4 / 0.6 (pagerank-favored): PageRank captures structural
# centrality (the node is connected to other central things); node
# importance captures Jukka-perspective standing (the node matters
# to him). They're correlated but not identical — PR is more stable,
# importance is more current. The blend leans toward PR (0.6) because
# importance has historically been NULL for newly-promoted nodes; the
# PR weighting keeps a sensible ordering even when importance is
# incomplete. As importance coverage improves, the weights could shift
# (eventually to e.g. 0.6 / 0.4 or beyond), but moving them is a
# calibrated decision, not a casual edit.
#
# Sum is 1.0 by convention; if you change them, keep the sum at 1.0
# so the blended score stays in the same 0-10 range as its inputs.
LENS_BLEND_IMPORTANCE_WEIGHT: float = 0.4
LENS_BLEND_PAGERANK_WEIGHT: float = 0.6

# CARD_HIGH_IMPORTANCE_FLOOR
# --------------------------
# What it gates: card-worthiness in builder.py. The legacy gate was
# observation_count + degree only; high-importance Entities that
# happened to be observed once with low degree were excluded even
# when the graph strongly says they matter. This threshold gives them
# a fast-path admission: an Entity above this floor gets a card
# regardless of observation count.
#
# Why 7.0: clear "this is a defining entity" tier. Below 7, the gate
# still wants the re-observation signal (the original "user keeps
# bringing this up" check); at 7+, importance alone is strong enough
# that we trust the graph topology.
CARD_HIGH_IMPORTANCE_FLOOR: float = 7.0

# CARD_MIN_DEGREE
# ---------------
# What it gates: the re-observation card-worthiness path. An Entity passes
# the multi-window observation gate only when ALSO embedded in the graph
# at this degree or above. Re-observation alone is too weak — common nouns
# like "AC", "Lights", "Computer", "Yogurt", "School" rack up multi-day
# observation counts by being used in normal English, not by being card-
# worthy entities.
#
# Why 15: calibrated against 2026-05-26 audit. Real card-worthy entities
# scored well above (Coffee 27, Dogs 29, Irvine 31, family 200+). The
# common-noun bloat (AC 12, Home 10, Lights 7, Computer 4, Yogurt 3,
# Calendar 2, School 6, House 6, Agent 6, Prompt 7, Agents 9, Downstream
# Agents 8, Nest Thermostat 8, People 3, System 5) sits below.
CARD_MIN_DEGREE: int = 15

# WIKI_GROWTH_IMPORTANCE_FLOOR
# ----------------------------
# What it gates: is_wiki_growth_candidate. Entities pass the wiki
# growth selector only if BOTH effective_importance >= this AND
# degree >= WIKI_GROWTH_MIN_DEGREE.
#
# Why 5.0: drops the Dairy-Aisle-class fan-out artifacts (imp ~4.5)
# while keeping moderately-important entities. Calibrated against
# real cases: Marika, Susie, Phil's mother are above; Dairy Aisle,
# Deli Section, Bread Aisle are below.
WIKI_GROWTH_IMPORTANCE_FLOOR: float = 5.0
WIKI_GROWTH_MIN_DEGREE: int = 4

# WIKI_REFRESH_CHANGE_FLOOR
# -------------------------
# What it gates: in wiki_generator/nightly_refresh.py, a node-content-
# change triggers wiki section refresh only when the changed node's
# importance is at or above this floor. Below, the wiki page stays
# stale until next full rebuild — saves regen cost on low-impact
# edits.
#
# Why 4.0: matches the previous CHANGE_IMPORTANCE_THRESHOLD constant
# value (moved here, value unchanged).
WIKI_REFRESH_CHANGE_FLOOR: float = 4.0

# STATE_DECAY_NOTEWORTHY_FLOOR
# ----------------------------
# What it gates: step_state_decay.py decides whether a State closure
# also writes a kg_maintenance_finding for user review. Short-term
# / low-importance closures happen silently; closures at or above
# this importance get a review row even when the duration class is
# routine.
#
# Why 5.0: matches the previous NOTEWORTHY_IMPORTANCE_FLOOR constant
# value (moved here, value unchanged).
STATE_DECAY_NOTEWORTHY_FLOOR: float = 5.0

# ── Gates ──────────────────────────────────────────────────────────────────


def is_card_worthy(node: "Node", *, degree: int | None = None) -> bool:
    """Should this entity get its own card?

    Entity-only. Canonical home for the gate previously inline at
    builder.py:106-112.

    Policy — pass if ANY of:
        - locked_by_user_at IS NOT NULL              (user-pinned)
        - observation_count >= 2 AND degree >= CARD_MIN_DEGREE
                                                     (re-observed AND
                                                     embedded in graph)
        - observation_count == 1 AND degree >= 10    (one deep window)
        - effective_importance >= CARD_HIGH_IMPORTANCE_FLOOR
                                                     (graph says it's defining)

    The observation-only gate (`obs >= 2 → pass`) was too permissive:
    common nouns ("AC", "Lights", "Computer", "Yogurt", "School") rack
    up multi-day observation counts just by being used in normal English,
    not by being card-worthy entities. Real entities have BOTH
    re-observation AND substantial graph presence — they accumulate
    edges to people/places/events around them. The conjunction filters
    out the bloat.

    The single-deep-window gate (obs==1 AND degree>=10) handles the
    legit case of a newly-introduced entity with rich first-mention
    relations. The importance fast-path handles sparse-but-defining
    entities whose graph hasn't grown yet but rater says it matters.

    Pass `degree` if you've already computed it; otherwise the function
    will fetch it from the node's outgoing+incoming edges. Cheap query
    but worth caching when iterating.
    """
    if getattr(node, "node_type", None) != "Entity":
        return False
    if getattr(node, "locked_by_user_at", None) is not None:
        return True
    obs = getattr(node, "observation_count", None) or 0
    if obs >= 2:
        if degree is None:
            degree = _compute_degree(node)
        if degree >= CARD_MIN_DEGREE:
            return True
    if obs == 1:
        if degree is None:
            degree = _compute_degree(node)
        if degree >= 10:
            return True
    # Importance fast-path: high effective importance alone is enough.
    # Local import to avoid a circular reference (effective imports
    # damping, both are in this package).
    from app.assistant.importance.effective import effective_importance
    if effective_importance(node) >= CARD_HIGH_IMPORTANCE_FLOOR:
        return True
    return False


def is_wiki_growth_candidate(node: "Node", *, degree: int | None = None) -> bool:
    """Should this entity get a new wiki page (when wiki_growth runs)?

    Entity-only. Policy: pass iff BOTH
        - degree >= WIKI_GROWTH_MIN_DEGREE
        - effective_importance >= WIKI_GROWTH_IMPORTANCE_FLOOR

    Why both gates: degree alone admits fan-out artifacts (Dairy Aisle
    has 15 same-pattern edges from one chat); importance alone would
    pull in sparse high-importance entities that don't have enough
    surrounding material to write a real wiki page about. The
    conjunction is what filters out both failure modes.

    Pass `degree` if you've already computed it; otherwise this fetches
    it from the node's edges.
    """
    if getattr(node, "node_type", None) != "Entity":
        return False
    if degree is None:
        degree = _compute_degree(node)
    if degree < WIKI_GROWTH_MIN_DEGREE:
        return False
    from app.assistant.importance.effective import effective_importance
    return effective_importance(node) >= WIKI_GROWTH_IMPORTANCE_FLOOR


def is_state_decay_priority(node: "Node") -> bool:
    """Should this State get prioritized for decay-close evaluation?

    State-only. Higher-importance States get evaluated first by the
    state-decay routine so the most-impactful staleness gets caught
    promptly. The threshold is intentionally low — decay is gentle and
    we want broad coverage; importance just orders the queue.

    Currently a placeholder for the same reason as wiki_growth — the
    existing routine reads node.importance directly and doesn't go
    through this gate yet. Moving it through here is a follow-up.
    """
    raise NotImplementedError(
        "state_decay priority not yet routed through importance module; "
        "currently reads node.importance directly in step_state_decay.py"
    )


def is_goal_active(node: "Node") -> bool:
    """Should this Goal still be treated as active?

    Goal-specific. Combines:
        - goal_status (open / paused / abandoned / achieved)
        - last_pursued_at (recency of mention)

    A Goal that was open but hasn't been pursued in 90 days might
    deserve dormancy. A Goal explicitly marked `abandoned` is no longer
    active regardless of importance.

    Placeholder — currently `goal_dormancy_sweep` reads these fields
    directly. Migrate once tests + telemetry exist.
    """
    raise NotImplementedError(
        "Goal-active classification not yet routed through importance module; "
        "currently inline in goal_dormancy_sweep"
    )


# ── Private helpers ────────────────────────────────────────────────────────


def _compute_degree(node: "Node") -> int:
    """Count of incident edges. Used by is_card_worthy when degree
    isn't pre-computed. Opens its own session — caller can avoid this
    overhead by passing `degree` directly.
    """
    from app.models.base import get_session
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge

    session = get_session()
    try:
        return (
            session.query(Edge)
            .filter((Edge.source_id == node.id) | (Edge.target_id == node.id))
            .count()
        )
    finally:
        session.close()
