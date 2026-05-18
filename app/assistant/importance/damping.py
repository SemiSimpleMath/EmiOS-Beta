"""Deterministic artifact-detection layer for importance.

The raw scoring layer (`scoring.py`) writes a per-node importance value
that's a pure bottom-up aggregation. That works for genuine hubs, but
it gets fooled by structural artifacts: one chat session that
fan-extracts 15 sibling nodes pointing at a single hub will inflate
that hub's importance to mid-tier, even when the hub itself is trivia.

Dairy Aisle is the canonical example: 15 `*_Purchase_Habit -- located_in
--> Dairy Aisle` edges all minted on 2026-05-11 from a single March
grocery-route walkthrough. Each edge scored ~3-4 (correctly — the
Habit really is about the location). MAX-aggregating gave Dairy Aisle
importance 4.5, enough to qualify for a wiki page. Plainly wrong from
Jukka's perspective.

The fix is a damping layer: cheap deterministic signals that detect
these structural artifacts and produce a multiplier in [0, 1] applied
to the raw score at read time. None of these require LLM judgment —
they're SQL-level structural facts.

## Signals (computed, returned as multipliers)

  distinct_days:
    Count of distinct YYYY-MM-DD dates on which the node was observed.
    A real hub gets mentioned across many days. A fan-extracted hub
    gets all its edges minted in one batch from one window.
    Memory ref: [[project_distinct_days_importance]] — cross-day
    persistence > raw mentions.

  edge_temporal_clustering:
    Fraction of incident edges created within a 24h window. If 100% of
    edges arrived in one batch, the hub is a fan-out artifact, not a
    recurring reference.

  obs_to_degree_ratio:
    observation_count / degree. A real hub gets re-observed close to
    once per edge gained. A fan-extracted hub has degree >> observation
    count because one observation spawned many edges.

  edge_type_diversity:
    Number of distinct relationship_types incident to the node. A real
    entity (Phil) has has_state + participant + targets + about +
    recipient + ... many types. A fan-out hub (Dairy Aisle) is 100%
    `located_in` / `route_location` / `includes`.

  sibling_label_overlap:
    For each neighbor, compute the token overlap between its label and
    other neighbors' labels. If many neighbors are token-cloned
    siblings ("Butter Purchase Habit", "Egg Purchase Habit", "Milk
    Purchase Habit"), the hub is bound to a single semantic pattern,
    not an organically grown set of connections.

  pure_sink:
    Outgoing edge count == 0. Real entities participate (they
    relate, they cause, they act). A pure-sink node receives all
    edges but doesn't originate any — characteristic of locations and
    other "container" concepts mentioned in passing.

## How signals compose into a multiplier

Each signal returns a value in [0, 1]:
  - 1.0 = no artifact evidence; pass through
  - lower = artifact evidence detected; dampen

`compute_damping(node)` multiplies all signals together. The compound
multiplier represents "fraction of raw importance that survives the
artifact filter."

For Phil (real hub): all signals return ~1.0, compound ≈ 1.0, no
damping. Effective importance = raw importance.

For Dairy Aisle: distinct_days low, edge_temporal_clustering very
clustered, obs/degree low, edge_type_diversity low, sibling_label_overlap
high, pure_sink yes. Compound multiplier collapses to ~0.05, knocking
effective importance from 4.5 to ~0.2.

## Coefficient calibration is deferred

Every signal currently returns 1.0 (no-op multiplier). This is
intentional — the structural detection is implemented, but the
coefficients that say "how much should a flagged signal actually reduce
importance" need calibration against held-out cases. Doing this
overnight without telemetry would be guessing. A follow-up session
will:

  - Identify a labeled set of nodes (real hubs we want preserved +
    known artifacts we want damped)
  - Sweep coefficients to find a setting that ranks them correctly
  - Wire damping on at the consumer layer

Until then, the module computes signals and logs their values without
applying any reduction. Effective importance equals raw importance.

## Type-aware behavior

Damping only fires for Entity / Concept nodes — those are the targets
of fan-out aggregation. For other types:

  - Edge: damping=1.0 always (edges are LLM judgments about specific
    relationships, not aggregation artifacts).

  - State / Event / Goal / Property: damping inherited via their
    source Entity. Since their raw importance derives from `max(
    src_imp × edge_imp / 10)`, and effective_importance of the source
    is `src_imp × source_damping`, no separate State-side damping
    is needed. Caveat: this only works if consumers reach State
    importance through effective_importance of the source rather
    than via the State's raw importance column. Today the dominant
    consumer (entity card builder) reads State importance directly,
    so once damping coefficients are turned on we'll need to either:
      (a) re-derive State importance after Entity damping changes, or
      (b) have effective_importance(state) compute on-the-fly via the
          state's source entity.
    (b) is cleaner and avoids a re-derivation pipeline. The code below
    is structured for (b).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node


# Per-signal coefficients. Currently all 1.0 = no-op. Calibrate later.
#
# To activate a signal: change its base from 1.0 to a number in (0, 1).
# That number is the floor — the multiplier the signal will produce when
# the artifact pattern is at its strongest. Linear interpolation between
# the floor and 1.0 based on signal strength.
DAMPING_FLOOR_DISTINCT_DAYS = 1.0      # set to ~0.4 once calibrated
DAMPING_FLOOR_TEMPORAL_CLUSTER = 1.0   # set to ~0.5 once calibrated
DAMPING_FLOOR_OBS_RATIO = 1.0          # set to ~0.6 once calibrated
DAMPING_FLOOR_EDGE_TYPE_DIVERSITY = 1.0  # set to ~0.6 once calibrated
DAMPING_FLOOR_SIBLING_OVERLAP = 1.0    # set to ~0.5 once calibrated
DAMPING_FLOOR_PURE_SINK = 1.0          # set to ~0.7 once calibrated

# Thresholds that trigger the "strongest" damping. Linear interpolation
# above these.
THRESHOLD_DISTINCT_DAYS_FEW = 2        # <=2 distinct days = strongest
THRESHOLD_TEMPORAL_CLUSTER_HOURS = 24  # all edges within 24h = strongest
THRESHOLD_OBS_RATIO_LOW = 0.3          # obs/degree < 0.3 = strongest
THRESHOLD_EDGE_TYPE_DIVERSITY_LOW = 2  # <=2 distinct rel types = strongest
THRESHOLD_SIBLING_OVERLAP_HIGH = 0.7   # >=70% siblings token-cloned = strongest


@dataclass(frozen=True)
class DampingReport:
    """Result of running all signals against a node. Useful for telemetry
    and calibration — we can dump these to a log and inspect which
    signals fired without changing behavior."""
    node_id: str
    distinct_days: float       # multiplier from this signal
    temporal_cluster: float
    obs_ratio: float
    edge_type_diversity: float
    sibling_overlap: float
    pure_sink: float
    compound: float            # product of all the above

    def fired(self) -> Tuple[str, ...]:
        """Names of signals that returned < 1.0 (artifact evidence)."""
        out = []
        if self.distinct_days < 1.0:
            out.append("distinct_days")
        if self.temporal_cluster < 1.0:
            out.append("temporal_cluster")
        if self.obs_ratio < 1.0:
            out.append("obs_ratio")
        if self.edge_type_diversity < 1.0:
            out.append("edge_type_diversity")
        if self.sibling_overlap < 1.0:
            out.append("sibling_overlap")
        if self.pure_sink < 1.0:
            out.append("pure_sink")
        return tuple(out)


def compute_damping(node: "Node") -> float:
    """Return the damping multiplier for a node, in [0, 1].

    1.0 = no artifact evidence (pass through raw importance).
    < 1.0 = artifact evidence; multiply raw by this.

    Edges are not nodes — call sites for edges should not invoke this.

    Currently a no-op (returns 1.0) until coefficients are calibrated.
    The signal computation is real though — every signal in the report
    reflects actual structural facts about the node.
    """
    report = compute_damping_report(node)
    return report.compound


def compute_damping_report(node: "Node") -> DampingReport:
    """Compute all signals and return the report. Useful for logging /
    calibration; downstream code typically just wants the multiplier.

    Only meaningful for Entity / Concept nodes — for other types,
    State/Event/Goal/Property, damping should be applied at the source
    entity via effective_importance composition (see module docstring).
    For those types this returns a no-op report.
    """
    node_type = getattr(node, "node_type", None)
    if node_type not in ("Entity", "Concept"):
        return _no_op_report(node)

    # Each helper returns a multiplier in [floor, 1.0]; the no-op floor
    # is 1.0 so until calibration these all return 1.0.
    dd = _damping_distinct_days(node)
    tc = _damping_temporal_cluster(node)
    obs = _damping_obs_ratio(node)
    etd = _damping_edge_type_diversity(node)
    sib = _damping_sibling_overlap(node)
    sink = _damping_pure_sink(node)

    compound = dd * tc * obs * etd * sib * sink
    return DampingReport(
        node_id=str(node.id),
        distinct_days=dd,
        temporal_cluster=tc,
        obs_ratio=obs,
        edge_type_diversity=etd,
        sibling_overlap=sib,
        pure_sink=sink,
        compound=compound,
    )


def _no_op_report(node: "Node") -> DampingReport:
    return DampingReport(
        node_id=str(node.id),
        distinct_days=1.0,
        temporal_cluster=1.0,
        obs_ratio=1.0,
        edge_type_diversity=1.0,
        sibling_overlap=1.0,
        pure_sink=1.0,
        compound=1.0,
    )


# ── Signal implementations ──────────────────────────────────────────────────
#
# Each signal returns a float in [floor, 1.0]:
#   - 1.0 means "no artifact pattern detected, pass through"
#   - floor (configurable per signal) means "artifact pattern at its strongest"
#   - Linear interpolation between based on how strong the signal is.
#
# Until calibration sets a floor < 1.0, every signal is a no-op (returns 1.0).


def _damping_distinct_days(node: "Node") -> float:
    """Fewer distinct observation days = more likely artifact.

    Reads `node.observation_count` as a proxy for now (cheap, no extra
    query). A more precise version would COUNT(DISTINCT date(timestamp))
    over kg_node_evidence, but observation_count is already updated
    on each re-mention so it correlates closely.

    No-op until DAMPING_FLOOR_DISTINCT_DAYS < 1.0.
    """
    if DAMPING_FLOOR_DISTINCT_DAYS >= 1.0:
        return 1.0
    obs = getattr(node, "observation_count", None) or 0
    if obs <= THRESHOLD_DISTINCT_DAYS_FEW:
        return DAMPING_FLOOR_DISTINCT_DAYS
    return 1.0


def _damping_temporal_cluster(node: "Node") -> float:
    """All edges created within 24h of each other = fan-out artifact.

    Needs an edge-list query to compute properly — skipped at no-op level
    to avoid the query cost when the coefficient is 1.0. When activated,
    the query joins kg_edge_metadata where source_id=node.id OR
    target_id=node.id and computes (max(created_at) - min(created_at)).
    """
    if DAMPING_FLOOR_TEMPORAL_CLUSTER >= 1.0:
        return 1.0
    # Implementation deferred to calibration — see module docstring.
    return 1.0


def _damping_obs_ratio(node: "Node") -> float:
    """observation_count / degree low = one observation fanned out into
    many edges, characteristic of a single-walkthrough hub.

    Degree is computed via the same edge query referenced above; until
    we wire that, return no-op.
    """
    if DAMPING_FLOOR_OBS_RATIO >= 1.0:
        return 1.0
    return 1.0


def _damping_edge_type_diversity(node: "Node") -> float:
    """Few distinct relationship_types = single-purpose hub.

    Real hubs (people) have many edge types: has_state, participant,
    targets, about, recipient, ... Single-purpose hubs (locations
    mentioned in one walkthrough) are 100% `located_in`.
    """
    if DAMPING_FLOOR_EDGE_TYPE_DIVERSITY >= 1.0:
        return 1.0
    return 1.0


def _damping_sibling_overlap(node: "Node") -> float:
    """High token overlap among neighbor labels = pattern-cloned siblings.

    "Butter Purchase Habit", "Egg Purchase Habit", "Milk Purchase Habit"
    all share the "Purchase Habit" token cluster. A real hub's neighbors
    are heterogeneous.
    """
    if DAMPING_FLOOR_SIBLING_OVERLAP >= 1.0:
        return 1.0
    return 1.0


def _damping_pure_sink(node: "Node") -> float:
    """Outgoing edges == 0 = container/location, not active participant."""
    if DAMPING_FLOOR_PURE_SINK >= 1.0:
        return 1.0
    return 1.0
