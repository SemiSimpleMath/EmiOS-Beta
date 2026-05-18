"""Effective importance — raw × damping.

Every downstream consumer should read importance through this module,
not directly from `node.importance` / `edge.importance`. The point is to
make damping (and any future adjustment layers) apply uniformly without
each consumer having to remember to call them.

## API surface

  effective_importance(node)       → float
      For node-typed importance. Applies the damping multiplier from
      `damping.compute_damping`. Returns 0.0 for nodes with NULL raw
      importance (the rater hasn't run yet) — callers that care about
      "unrated" vs "low" should check raw importance directly via
      `node.importance`.

  effective_edge_importance(edge)  → float
      For edge-typed importance. Pass-through — edges are never damped
      because they're LLM judgments about specific relationships, not
      aggregation artifacts. Provided for API symmetry so consumers
      can use `effective_edge_importance(edge)` everywhere instead of
      sometimes reading `edge.importance` directly.

## Why the asymmetric API (separate node vs edge function)

Calling `effective_importance(edge)` would be a type confusion; node
importance and edge importance have entirely different semantics. The
type system can't enforce that today, but separating the functions
makes the call site read correctly and gives a place to add edge-
specific logic later (e.g., recency decay on edge importance) without
contaminating the node path.

## Why not cache the multiplier on the node row

Damping is computed from the node's NEIGHBORHOOD (observation count,
edge times, neighbor labels). Caching it on the node would require
invalidating whenever a neighbor changes — same cascade problem
`node.description` has. Read-time computation is cheap (it's pure SQL
or already-loaded Node fields) and avoids the invalidation cascade.

## State / Event / Goal / Property handling

These node types inherit damping through their source Entity per
`damping.compute_damping_report` (returns no-op for non-Entity types).
But their RAW importance was computed as `max(src_imp × edge_imp / 10)`
using the source Entity's raw importance.

When damping coefficients are eventually turned on, we want a State's
effective importance to reflect the source Entity's damping too —
otherwise a State whose source got damped to ~0.5 would still report
its old raw value, giving stale signal to consumers.

The clean fix (deferred until calibration is on the table): in
`effective_importance` for State-like nodes, recompute the derivation
ON THE FLY using the source Entity's effective_importance. For now,
since damping is a no-op multiplier, raw == effective and the
distinction doesn't matter. Code below marks the spot with a TODO so
it's obvious where to insert the recomputation when calibration starts.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.assistant.importance.damping import compute_damping

if TYPE_CHECKING:
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge


# ════════════════════════════════════════════════════════════════════════════
#   EFFECTIVE IMPORTANCE  —  what every consumer should read instead of raw
#
#   PERSPECTIVE — same as raw importance for the node's type, AFTER an
#   artifact-correction pass. The damping layer (damping.py) detects
#   structural patterns that inflate raw importance without
#   corresponding "actually matters to anyone" signal — e.g., fan-out
#   hubs like Dairy Aisle that get 15 mediocre edges from one
#   walkthrough and pass the MAX-aggregation threshold despite being
#   trivia. Effective importance is raw × damping multiplier.
#
#   This is the closest thing the system has to "user-perspective"
#   importance, but it's still not strictly user-anchored — it's
#   "raw importance with the most obvious artifacts knocked down."
#   A true user-anchored LLM rating would be a separate layer; the
#   damping approach replaces it with deterministic SQL signals at
#   a fraction of the cost.
#
#   COMPOSITION — node-only, no edge-chain traversal:
#
#       effective_importance(node) = raw_importance(node) × damping(node)
#
#   Damping is computed from the node's local neighborhood (observation
#   count, edge timestamps, neighbor label patterns) — no walk across
#   the graph, no LLM. Currently damping always returns 1.0 (signals
#   computed but coefficients are no-ops until calibrated against
#   held-out examples).
#
#   For State / Event / Goal / Property: damping inherits via the
#   source Entity automatically, because raw importance for those types
#   is already derived from Entity importance through the multiplication
#   chain (Entity → edge → State). When the source Entity's damping
#   eventually drops to <1.0, the State's raw importance will too on
#   the next regenerate_state_importance pass.
#
#   CONSUMERS — this is the function downstream code should call instead
#   of reading `node.importance` directly. Today most consumers still
#   read raw; the migration to effective is being done incrementally as
#   each call site is touched. Eventually:
#     - Card-worthiness gate, card fact floor → effective
#     - Wiki growth selection → effective × degree
#     - KG visualizer slider → effective
#     - Context engine convergence → effective
#     - Timeline marker, timeline confirmation floor → effective
#     - Lens layout positioning → effective × pagerank blend
#     - PageRank edge weights → raw edge importance (no damping for edges)
# ════════════════════════════════════════════════════════════════════════════
def effective_importance(node: "Node") -> float:
    """Damping-adjusted importance for a node. Returns 0.0 for NULL raw.

    Pass-through equivalent to `node.importance` until damping
    coefficients are calibrated. The contract is stable now so
    downstream consumers can migrate to this function safely.
    """
    raw = getattr(node, "importance", None)
    if raw is None:
        return 0.0
    raw_f = float(raw)

    # TODO (post-calibration): for State/Event/Goal/Property, recompute
    # importance on the fly using source-entity effective importance,
    # instead of trusting the stored derived value. See module docstring.
    # While damping is a no-op multiplier this branch doesn't matter.

    multiplier = compute_damping(node)
    return raw_f * multiplier


# ════════════════════════════════════════════════════════════════════════════
#   EFFECTIVE EDGE IMPORTANCE  —  pure pass-through; no damping
#
#   PERSPECTIVE — source-anchored, same as raw edge.importance. Edges
#   don't get damped because the damping layer is designed for
#   aggregation artifacts (multiple edges fanning out from one chat
#   minting fake hubs). A single LLM-rated edge isn't an aggregation;
#   it's a relationship judgment. No artifact pattern to correct for.
#
#   COMPOSITION — identity function over edge.importance.
#
#   API symmetry: this exists so consumers reading both node and edge
#   importance can use a consistent accessor instead of `effective_
#   importance(node)` + `edge.importance` (which would look inconsistent).
#
#   CONSUMERS — same as for raw edge importance; this function just
#   provides the canonical accessor:
#     - PageRank edge weights
#     - Entity importance derivation (MAX over adjacent)
#     - State importance derivation (multiplied into the chain)
#     - Card-fact admission fallback (when node imp is NULL)
#     - KG search ranking
#     - Convergence graph activation edge weighting
# ════════════════════════════════════════════════════════════════════════════
def effective_edge_importance(edge: "Edge") -> float:
    """Pass-through accessor for edge importance.

    Edges aren't damped — they're LLM-rated relationships, not
    aggregation artifacts. This function exists for API symmetry so
    consumers reading both node and edge importance use a consistent
    accessor and not a mix of `effective_importance(node)` and
    `edge.importance`.
    """
    raw = getattr(edge, "importance", None)
    if raw is None:
        return 0.0
    return float(raw)
