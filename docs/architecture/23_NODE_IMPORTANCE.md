# Node and edge importance

How the KG decides which nodes/edges matter to the user, who scores them,
when, and where the gaps are.

## What gets rated

| Subject | Rater | When | Scale |
|---|---|---|---|
| **Entity** nodes | `me::importance_rater` agent | `kg_importance_rater` routine (interval, every ~30 min, only_unrated) | 0–10 (per agent prompt), persisted to `kg_node_metadata.importance` |
| **kg_edge** rows | `me::edge_importance_rater` agent | same routine | 0–10 from source's perspective, persisted to `kg_edge_metadata.importance` |
| **State / Event / Goal / Property** nodes | **(no rater)** | — | column exists; populated only by ad-hoc paths (proposal promotion, manual writes) |

Code paths:
- Routine: `app/assistant/routine_manager/routine_functions.py` `_lazy_kg_importance_rater`
- Entity rater: `app/me/importance.py` `regenerate_importance()` — defaults `only_node_types=["Entity"]`
- Edge rater: `app/me/edge_importance.py` `regenerate_edge_importance()`
- Entity rater agent: `app/assistant/agents/me/importance_rater/`
- Edge rater agent: `app/assistant/agents/me/edge_importance_rater/`

Both rater agents live under `me::` because the lens being scored is the
relationship-from-the-user's-perspective lens.

## The gap: State / Event / Goal nodes

State (and Event, Goal, Property) importance values exist but **no agent
periodically re-rates them.** What's there came from inconsistent paths
at proposal promotion or earlier code that's no longer wired in.

Distribution snapshot (May 2026, 3,352 State nodes):

| State.importance | Count | % |
|---|---|---|
| NULL | 1,381 | 41% |
| exactly 0.5 (the documented `DEFAULT_SCORE`) | 98 | 3% |
| < 0.5 | 356 | 11% |
| > 0.5 | 1,517 | 45% |

`DEFAULT_SCORE = 0.5` lives at `app/assistant/kg_core/kg_utils/node_importance.py:29`
and gets used by every consumer that does `or DEFAULT_SCORE` when reading
the column. So a State at exactly 0.5 in the DB is effectively
indistinguishable from a State the rater never visited.

## The intended-but-unbuilt derivation

State importance is conceptually "how important is this State to the
entities it connects?" — i.e., a function of the importances of the
edges that touch it (which the edge rater scores from the source's
perspective). A State linked to Jukka and Katy by `is_married` edges
weighted 10.0 should rank ~10/10; a State linked to a peripheral entity
by a generic `topic` edge weighted 0.8 should rank low.

**There is no code that performs this derivation.** Nothing reads
`kg_edge_metadata.importance` to compute or backfill
`kg_node_metadata.importance` for a State node.

Verified empirically:

| State | state.importance | max edge.importance touching it |
|---|---|---|
| "Married to Katy" (Jukka–Katy) | 0.95 (rated somehow) | 10.0 |
| "Marriage" (Marika & Joe) | **0.50** (looks like default fallback) | — |
| "Residence — Irvine move" (`a4628fc3`) | NULL | 7.0 |

The Marika+Joe case is almost certainly an unrated State sitting at the
default — the user's surfaced concern that triggered this audit.

## Consumer behavior given the gap

The `kg_finding_priority` drain ordering (`app/assistant/kg_investigator/finding_priority.py`):
- Reads `state.importance` directly when set
- Falls back to `MAX(edge.importance) / 10` across edges touching the
  node when `state.importance IS NULL` — same signal the rater would
  use, computed inline so unrated nodes don't get buried

The wiki nightly refresh (`app/assistant/wiki_generator/nightly_refresh.py`):
- Defers re-rendering a page until `kg_importance_rater` has rated the
  node — but since States never get rated, this affects only Entities
  in practice.

The lens (`app/me/`) and entity-card pipeline both apply
`importance × 0.4 + pagerank × 0.6` blends with `or DEFAULT_SCORE` (0.5)
fallbacks. Half the State nodes contributing through this blend are
contributing the default rather than a real score.

## What would close the gap

A State/Event importance rater that runs in the existing
`kg_importance_rater` routine alongside the entity + edge raters. Two
viable shapes:

1. **Derived (deterministic).** A SQL aggregate: for each unrated
   State, set `importance = MAX(edge.importance) / 10` over its edges,
   gated on the edge rater having actually run. Cheap; assumes the edge
   rater's scoring is the right signal (it generally is — it's already
   the agreed-upon "from source's perspective" measure).
2. **LLM-rated (parallel to Entity).** A prompt similar to
   `me::importance_rater` but tuned for States (different calibration:
   life events vs preferences vs background facts). Pass
   `only_node_types=["State", "Event", "Goal"]` to `regenerate_importance`.

Option 1 is cheaper, deterministic, and aligned with the architectural
intent ("state importance comes from edge importance"). Option 2 is
more flexible but spends LLM tokens on something the edge data already
encodes.

Either way, the routine wiring change is small: extend
`_lazy_kg_importance_rater` to also rate States after edges are scored.
