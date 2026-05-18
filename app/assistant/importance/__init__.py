"""Unified importance facade for the KG.

Before this module, importance logic was scattered across 12+ files: each
consumer hardcoded its own threshold, each scorer lived next to whatever
pipeline first needed it, the "is X important enough" question got
re-answered with different conventions in different places. Tracing where
the Dairy Aisle fan-out hub got its 4.5 importance — and why that
number was good enough to qualify for both a card and a wiki page —
required four grep passes through unrelated subsystems. This module is
the consolidation.

## What's unified, what stays specialized

Three separate concerns lived together in the old layout. Pulling them
apart was the first step:

  1. SCORING — populating `kg_node_metadata.importance` and
     `kg_edge_metadata.importance`. Genuinely type-specialized: Entity
     importance derives from MAX(adjacent edge.importance); State/Event/
     Goal/Property importance derives from MAX(src_imp × edge_imp / 10);
     edge importance is LLM-rated from the source's perspective. These
     formulas have different semantics and *should not* be unified.
     `scoring.py` re-exports the type-specific writers from their
     existing locations.

  2. DAMPING — the deterministic artifact-detection layer that didn't
     exist before. Applied AFTER raw scoring, only at READ time, only
     for Entity-style consumers. State/Event/Goal/Property inherit
     damping through their source Entity automatically because their
     derivation reads the source's already-damped value. Edges are
     never damped (they're LLM-rated relationships, not aggregation
     artifacts). `damping.py` owns the signals.

  3. CONSUMPTION — every gate that READS importance to decide
     something (is-card-worthy, is-wiki-growth-candidate,
     is-state-decay-priority, section-tag-importance-floor,
     timeline-confirmation-floor, etc.). These were hardcoded
     thresholds spread across the codebase. They all live in
     `consumers.py` now, so changing a threshold is a one-file edit
     instead of a hunt.

## Why this shape

The three concerns separate cleanly along the axis "do you compute,
adjust, or read?" Scoring writes the column. Damping adjusts what
you'd read. Consumers act on the adjusted value. Each layer has
exactly one entry point per type, and the layers compose:

    raw_importance(node)
        |
        +-- damping(node) ----+
                              v
                  effective_importance(node)
                              |
        is_card_worthy(node) -+
        is_wiki_growth_candidate(node) -+
        importance_marker(node) -+
        ... every consumer reads `effective_importance`, never raw.

## Type specialization that REMAINS

Despite the consolidation, three places keep type awareness:

  - Damping signals only fire for Entity nodes. State/Event/Goal/Property
    inherit through their parent Entity's damping. Edges always
    damping=1.0. This is in `damping.py` as a single type-dispatch.

  - Card-worthiness is Entity-only (only Entity nodes get cards per
    today's title-uniqueness + node-type invariants). Hardcoded in
    `consumers.is_card_worthy` via an explicit `node_type == 'Entity'`
    check.

  - Goal-specific lifecycle (status, last_pursued_at) factors into Goal
    consumers via a Goal-only path in `consumers.is_goal_active`.
    Keeps Goal's special biological semantics from contaminating the
    rest of the API.

Every other call site can treat nodes uniformly.

## What was UNIFIED in this commit (the migration log)

### Constants and helpers moved (no compatibility aliases)

  - `IMPORTANCE_WEIGHT` / `PAGERANK_WEIGHT` blend constants
        was: `kg_core/kg_utils/node_importance.py`
        now: `consumers.LENS_BLEND_IMPORTANCE_WEIGHT` /
             `consumers.LENS_BLEND_PAGERANK_WEIGHT`
        callers updated: `step_description_fill`, `node_importance`
                         (in-file usage), and the consumer renamed inline.

  - `DEFAULT_IMPORTANCE_THRESHOLD` / `DEFAULT_SECOND_WAVE_IMPORTANCE`
        was: `context_engine/context_activation.py`
        now: `consumers.CONTEXT_ACTIVATION_THRESHOLD` /
             `consumers.CONTEXT_ACTIVATION_SECOND_WAVE_THRESHOLD`
        callers updated: `context_activation` (in-file usage).

  - `_MIN_IMPORTANCE_FOR_CONFIRMATION` for timeline gaps
        was: private const in `kg/timeline_gaps.py`
        now: `consumers.TIMELINE_CONFIRMATION_FLOOR`
        callers updated: `timeline_gaps` (in-file usage).

  - `CARD_FACT_IMPORTANCE_FLOOR` / `CARD_FACT_MIN_KEEP`
        was: inline in `pipelines/entity_cards_v2/builder.py`
        now: `consumers.CARD_FACT_FLOOR` / `consumers.CARD_FACT_MIN_KEEP`
             (renamed `CARD_FACT_IMPORTANCE_FLOOR` → `CARD_FACT_FLOOR`).
        callers updated: `builder` (in-file usage).

  - `_BAND_MAJOR` / `_BAND_MID` display bands and `_importance_marker`
        was: private in `kg/timeline_builder.py`
        now: `display.IMPORTANCE_BAND_MAJOR`, `display.IMPORTANCE_BAND_MID`,
             `display.importance_marker`
        callers updated: `timeline_builder` (in-file usage).

  - `_source_entity_importance` helper
        was: private in `kg/section_tagging.py`
        now: `queries.max_source_entity_importance` (verbatim port)
        callers updated: `section_tagging` (in-file usage).

  - Scoring writers `regenerate_edge_importance`,
    `regenerate_entity_importance`, `regenerate_state_importance`
        was: `app/assistant/kg/edge_importance.py`,
             `app/me/importance.py`
        now: `scoring.py` (full implementations, not re-exports)
        callers updated: `routine_manager/routine_functions.py`,
                         the scoring module's own `__main__` block.

  - Importance cache `get_importance_map` / `get_importance` /
    `invalidate` / `DEFAULT_SCORE` / `IMPORTANCE_PATH`
        was: `app/me/importance.py`
        now: `cache.py` (full implementations)
        callers updated: `app/me/api.py`, `app/me/layout.py`,
                         `app/me/pagerank.py`.

### Files deleted in the same commit

  - `app/assistant/kg/edge_importance.py` (replaced by `scoring.py`)
  - `app/me/importance.py` (replaced by `scoring.py` + `cache.py`;
    the deprecated LLM Entity rater `regenerate_importance` had no
    remaining production callers and is also gone)

### Still inline (not yet routed through the module)

  - The card-worthiness gate in `builder.py:106-112` lives inline.
    `consumers.is_card_worthy(node)` exists as the canonical gate but
    refactoring the call site requires session-passing rework. The
    behavior is preserved; the call-site refactor is a follow-up.

  - `wiki_growth` selection uses `min_degree: 4` from routine config —
    importance plays no role. `consumers.is_wiki_growth_candidate`
    raises `NotImplementedError` to mark the intended replacement.

  - `view_materializer_node._importance_sort_key` reads a categorical
    "low"/"medium"/"high" string from dayflow items, not the KG numeric
    importance. Different domain; not unified.

  - The email-importance threshold in `DataConversion.py` (hardcoded
    `5`) is a different domain; not unified.

### Calibration intentionally deferred

Damping signals are implemented in `damping.py` but every coefficient
defaults to 1.0 (no-op multiplier). The signal computation is real;
the reduction step is a no-op until coefficients are calibrated against
held-out cases (Dairy Aisle should score ~0.5; Phil ~9). Calibrate in
a dedicated session with telemetry, not overnight.
"""
from app.assistant.importance.effective import (  # noqa: F401
    effective_importance,
    effective_edge_importance,
)
from app.assistant.importance.damping import (  # noqa: F401
    compute_damping,
)
from app.assistant.importance.consumers import (  # noqa: F401
    # Thresholds (formerly scattered):
    CARD_FACT_FLOOR,
    CARD_FACT_MIN_KEEP,
    CARD_HIGH_IMPORTANCE_FLOOR,
    CONTEXT_ACTIVATION_THRESHOLD,
    CONTEXT_ACTIVATION_SECOND_WAVE_THRESHOLD,
    TIMELINE_CONFIRMATION_FLOOR,
    LENS_BLEND_IMPORTANCE_WEIGHT,
    LENS_BLEND_PAGERANK_WEIGHT,
    WIKI_GROWTH_IMPORTANCE_FLOOR,
    WIKI_GROWTH_MIN_DEGREE,
    WIKI_REFRESH_CHANGE_FLOOR,
    STATE_DECAY_NOTEWORTHY_FLOOR,
    # Gates:
    is_card_worthy,
    is_wiki_growth_candidate,
    is_state_decay_priority,
    is_goal_active,
)
from app.assistant.importance.display import (  # noqa: F401
    importance_marker,
    IMPORTANCE_BAND_MAJOR,
    IMPORTANCE_BAND_MID,
)
from app.assistant.importance.queries import (  # noqa: F401
    max_source_entity_importance,
)
from app.assistant.importance.cache import (  # noqa: F401
    get_importance_map,
    get_importance,
    invalidate,
)
from app.assistant.importance.scoring import (  # noqa: F401
    regenerate_edge_importance,
    regenerate_entity_importance,
    regenerate_state_importance,
)
