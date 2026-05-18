"""Scoring layer — writers that populate the raw importance columns.

These functions WRITE `kg_node_metadata.importance` and
`kg_edge_metadata.importance`. They are type-specialized by design and
should NOT be unified into one function.

## Why scoring stays type-specialized (not unified)

Different node types carry importance for different reasons. Forcing
them into one formula was the bug that put Dairy Aisle on equal footing
with Phil. The three formulas in play:

  Edge importance:
    LLM-rated by `me::edge_importance_rater` from the SOURCE's
    perspective. "How important is this edge to the source node?"
    e.g. `Jukka --addressee--> Emi` rated 9 because Jukka cares
    deeply about Emi. Per `[[feedback_edge_importance_source_perspective]]`.

  Entity / Concept importance:
    MAX(adjacent_edge.importance). The edges themselves directly
    express how important the entity is from someone else's POV —
    aggregating their maximum picks "the strongest reason anyone
    cares about this entity." Replaced an earlier LLM Entity rater
    on 2026-05-12 (commit f3135cb2) because the LLM kept misjudging
    AI tools / abstract concepts.

  State / Event / Goal / Property importance:
    MAX(src_imp × edge_imp / 10) over incoming edges. Inherits from
    the most-important participant, scaled by how much that edge
    matters to that participant. The /10 keeps the product in 0-10.
    State/Event don't get LLM-rated directly — they're connective
    tissue derived from the entities they connect.

These formulas have genuinely different semantics. Trying to unify
them under a single function would just hide the dispatch inside the
function body — same complexity, worse readability.

## History

The edge rater used to live at `app/assistant/kg/edge_importance.py`
and the node deriver at `app/me/importance.py`; both moved here on
2026-05-17 as part of the importance-module consolidation. Old
locations were deleted in the same commit. If you grep for those
paths in older commits you'll find the original implementations.

The deprecated LLM Entity rater (`regenerate_importance`) from
`app/me/importance.py` is also gone — replaced 2026-05-12 by
`regenerate_entity_importance` (the edge-MAX derivation below).
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


# Default per-edge score when the rater couldn't produce one (LLM omitted
# the edge from its output). Mid-scale — not "important," not "trivia."
DEFAULT_EDGE_SCORE = 5.0
EDGE_RATER_BATCH_SIZE = 50  # ~50 edges per LLM call


# ── Edge scoring (LLM-rated from source's perspective) ─────────────────────


def _build_edge_block(
    edge: Edge,
    node_by_id: Dict[str, Node],
) -> Optional[str]:
    """Multi-line text representation of one edge for the rater prompt.

    Each field on its own line so the model reads them cleanly. The
    EDGE SENTENCE is the most important signal — it's the one whose
    importance the rater is being asked to score — so it gets all-caps
    framing to make sure the model anchors on it.

    Returns None if either endpoint is missing — the edge is malformed.
    """
    src = node_by_id.get(str(edge.source_id))
    tgt = node_by_id.get(str(edge.target_id))
    if src is None or tgt is None:
        return None

    def _node_sentence(n: Node) -> str:
        """Canonical present-tense sentence written at promotion time.
        Falls back to description (a later-written projection that may be
        empty) only if original_sentence is itself missing."""
        sent = (getattr(n, "original_sentence", None) or "").strip()
        if not sent:
            sent = (n.description or "").strip()
        if not sent:
            return "-"
        if len(sent) > 240:
            sent = sent[:237] + "..."
        return sent

    edge_sent = (edge.sentence or "").strip()
    if len(edge_sent) > 280:
        edge_sent = edge_sent[:277] + "..."

    lines = [
        f"id: {edge.id}",
        f"source label: {src.label or '-'}",
        f"source type: {src.node_type or '-'}/{src.category or '-'}",
        f"source sentence: {_node_sentence(src)}",
        f"edge label: {edge.relationship_type or '-'}",
        f"target label: {tgt.label or '-'}",
        f"target type: {tgt.node_type or '-'}/{tgt.category or '-'}",
        f"target sentence: {_node_sentence(tgt)}",
        f"EDGE SENTENCE — THIS IS THE ONE WHOSE IMPORTANCE TO THE SOURCE YOU ARE EVALUATING: {edge_sent or '-'}",
    ]
    return "\n".join(lines)


def _write_edge_scores_batch(session: Session, scored: Dict[str, float]) -> None:
    """Update kg_edge.importance for a batch of edges in a single transaction."""
    if not scored:
        return
    edges = session.query(Edge).filter(Edge.id.in_(list(scored.keys()))).all()
    for e in edges:
        score = scored.get(str(e.id))
        if score is None:
            continue
        e.importance = float(score)


# ════════════════════════════════════════════════════════════════════════════
#   EDGE IMPORTANCE  —  perspective: the SOURCE node of the edge
#
#   PERSPECTIVE — single-anchored. An edge has a source and a target;
#   the rated importance is the SOURCE's view of the edge. The same
#   edge has a different importance for each side, but we store only
#   one number in the column. If a consumer needs "the other side's
#   view" it must walk to the reverse edge (if any exists) or accept
#   that we don't have that view.
#
#   NOT THE USER'S PERSPECTIVE (DIRECTLY). The edge rater doesn't know
#   who Jukka is and doesn't preferentially score edges from his POV.
#   It scores from whichever entity is the source of THAT edge. User-
#   perspective signal emerges in two ways: (1) most edges in the graph
#   ARE rooted at Jukka or someone close to him, so the source-POV
#   ratings mostly track Jukka-relevance by structural construction;
#   (2) the deprecated LLM Entity rater used to add a top-down user-
#   anchored layer — that's the gap the damping module fills now.
#
#   COMPOSITION — none. Each edge is rated standalone with one LLM call,
#   given the edge sentence + endpoint context. No chain-of-edge
#   traversal, no aggregation. The atomicity is by design: the rater's
#   job is "score this single relationship"; aggregations happen at
#   the node-importance layer using these per-edge values as inputs.
#
#   CONSUMERS — who reads edge.importance and what they do with it:
#     - PageRank (step_pagerank.py): edge weights for the PR iteration.
#       Important edges propagate more influence between endpoints.
#     - Entity importance (regenerate_entity_importance below): MAX
#       over an Entity's adjacent edge importances → Entity importance.
#     - State importance (regenerate_state_importance below): the
#       edge_imp factor in `MAX(src_imp × edge_imp / 10)` → State
#       importance.
#     - Card fact admission (_collect_candidate_facts): when a fact's
#       node importance is NULL, falls back to the edge importance.
#     - KG search ranking (knowledge_graph_search.py): retrieval order.
#     - Convergence graph activation (kg_convergence.py): edge weight
#       for the BFS expansion / score propagation.
#     - KG visualizer (graph_visualizer/api.py): serialized to clients
#       so the front-end can render thickness / opacity by edge weight.
# ════════════════════════════════════════════════════════════════════════════
def regenerate_edge_importance(
    *,
    batch_size: int = EDGE_RATER_BATCH_SIZE,
    only_unrated: bool = True,
) -> int:
    """Rate every edge and write to kg_edge_metadata.importance.

    SEMANTIC. The number this writes answers ONE question: "from the
    SOURCE node's perspective, how important is this relationship?"
    For `Jukka --addressee--> Emi` we're asking how important THIS
    edge is to JUKKA. Not how important to Emi, not how important in
    the abstract. The asymmetry matters: the same edge has a different
    importance for each endpoint, but we only store the source's view
    in this column. (When a consumer needs "from the other side's
    perspective" it gets it via the reverse edge if one exists.)

    WHY THIS SEMANTIC. Edges are anchored to a subject in natural
    language ("Jukka emails Emi", "Phil has phone number X") — the
    subject IS who cares. A scheme that tried to score from both
    endpoints simultaneously would have to invent a blending rule;
    we'd be guessing. Anchoring to the source matches how the data
    was written and makes the value interpretable.

    WHAT IT ACCOMPLISHES (downstream consumers):
      - PageRank edge weights: an important edge propagates more
        influence between endpoints. Without per-edge weighting all
        edges count equally and PR can't distinguish a major bond
        from a passing reference.
      - Entity / Concept node importance (see regenerate_entity_importance):
        MAX over adjacent edges. The strongest edge into a node sets
        the floor on how important the node itself is.
      - State / Event / Goal / Property node importance (see
        regenerate_state_importance): MAX(src_imp × edge_imp / 10).
        States inherit importance through edge importance.
      - The card section_tagger admission gate (`max_source_entity_importance`
        in queries.py): when there's no node-importance rating yet,
        falls back to MAX(edge.importance) / 10 over connecting edges.

    FAILURE MODES:
      - LLM bias: the source-perspective framing depends on whoever
        rates the edge sharing the source's evaluative lens. The rater
        has occasionally undervalued contact-info edges ("Phil's phone
        number") because they're not "important to Phil" — they're
        important to whoever wants to reach Phil. The contact carve-out
        in entity_cards (CARD_FACT_FLOOR bypass via section-tag) is
        the workaround.
      - Self-similar fan-out: 15 "X Purchase Habit --located_in-->
        Dairy Aisle" edges each get a reasonable 3-4 score because
        the Habit really IS about the location. The edges aren't
        wrong individually; they collectively inflate Dairy Aisle's
        derived node importance. See damping.py for the artifact-
        detection layer that addresses this without retouching the
        edge scoring.

    Args:
        batch_size: edges per LLM call.
        only_unrated: when True (default), skip edges that already have
            a non-NULL importance value. Set False to fully re-rate.

    Returns: number of edges scored this run.
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import (
        Message,
        ScopeApprovalPolicy,
        ScopeContext,
        ScopeResourcePolicy,
    )

    started = time.time()

    # Load all nodes once for label/category lookup; avoids per-batch joins.
    with get_db_manager().read_session() as session:
        node_by_id: Dict[str, Node] = {
            str(n.id): n for n in session.query(Node).all()
        }
        if only_unrated:
            all_edges = session.query(Edge).filter(Edge.importance.is_(None)).all()
        else:
            all_edges = session.query(Edge).all()

    logger.info(
        "edge importance: rating %d edges in batches of %d (only_unrated=%s)",
        len(all_edges), batch_size, only_unrated,
    )

    scope = ScopeContext(
        scope_id="scope::me::edge_importance",
        owner_id="jukka",
        actor_id="me_edge_importance_runner",
        surface="ui",
        room_id="me_lens",
        approval=ScopeApprovalPolicy(authority_level=99),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )

    rated_count = 0
    failures = 0

    for i in range(0, len(all_edges), batch_size):
        batch = all_edges[i : i + batch_size]
        prompt_lines: List[str] = []
        batch_ids: List[str] = []
        for e in batch:
            block = _build_edge_block(e, node_by_id)
            if block is None:
                continue
            prompt_lines.append(block)
            batch_ids.append(str(e.id))
        if not prompt_lines:
            continue
        batch_text = "\n".join(prompt_lines)

        agent = DI.agent_factory.create_agent("me::edge_importance_rater")
        if agent is None:
            logger.error("edge importance: agent unavailable")
            break

        msg = Message(
            agent_input={"task": batch_text, "information": ""},
            task=batch_text,
            information="",
            scope_context=scope,
        )
        try:
            result = agent.action_handler(msg)
            data = getattr(result, "data", None) or {}
            ratings = data.get("ratings") or []
            id_set = set(batch_ids)
            scored: Dict[str, float] = {}
            for r in ratings:
                if not isinstance(r, dict):
                    continue
                eid = str(r.get("id") or "").strip()
                if eid not in id_set:
                    continue
                try:
                    score = float(r.get("score") or DEFAULT_EDGE_SCORE)
                except (TypeError, ValueError):
                    continue
                scored[eid] = max(0.0, min(10.0, score))

            for eid in batch_ids:
                scored.setdefault(eid, DEFAULT_EDGE_SCORE)

            with get_db_manager().transaction(op="me_edge_importance") as session:
                _write_edge_scores_batch(session, scored)
            rated_count += len(scored)

            logger.info(
                "edge importance: batch %d/%d → %d ratings (running total %d)",
                (i // batch_size) + 1,
                (len(all_edges) + batch_size - 1) // batch_size,
                len(scored),
                rated_count,
            )
        except Exception as e:
            failures += 1
            logger.error("edge importance: batch %d failed: %s", (i // batch_size) + 1, e)
            if failures >= 5:
                logger.error("edge importance: too many failures, aborting")
                break

    elapsed = time.time() - started
    logger.info(
        "edge importance: rated %d edges in %.1fs (%d failures)",
        rated_count, elapsed, failures,
    )
    return rated_count


# ── Node scoring (derived from edge importance) ────────────────────────────


# ════════════════════════════════════════════════════════════════════════════
#   ENTITY / CONCEPT IMPORTANCE  —  perspective: "any participant, MAX"
#
#   PERSPECTIVE — multi-anchored, max-aggregated. An Entity's importance
#   is the SINGLE strongest reason anyone cares about it. It does NOT
#   answer "how important is X to Jukka specifically" — it answers "is
#   there ANY perspective from which X matters?" In a graph where most
#   edges are anchored on Jukka or his circle, this coincides with
#   Jukka-relative importance for most nodes; for nodes deep in someone
#   else's subgraph it does not.
#
#   This is the place where "user POV" is currently missing as a
#   distinct layer. The deprecated LLM Entity rater was supposed to
#   provide it; damping signals (damping.py, calibration deferred)
#   are the planned replacement that knock down structural artifacts
#   like Dairy Aisle that pass the max-aggregation gate but fail any
#   reasonable "matters to Jukka" check.
#
#   COMPOSITION — single-hop, MAX over edges:
#
#       Entity.importance = MAX(edge.importance) over adjacent edges
#
#   No edge chains. No multiplication. The strongest single edge
#   touching this Entity becomes its importance. Both directions
#   (incoming and outgoing) count.
#
#   Why MAX vs sum: sum rewards fan-out (15 mediocre edges beat 1
#   critical edge). Max anchors on "the strongest reason anyone cares."
#   See WHY block in the docstring for the calibration-against-real-
#   examples reasoning.
#
#   CONSUMERS — who reads Entity.importance and what they do with it:
#     - Card-worthiness gate (builder.py:106-112): the gate ITSELF
#       reads observation_count + degree (not importance), so an
#       Entity is admitted to "should this have a card?" before we
#       look at its importance. Once admitted, importance enters the
#       FACT FLOOR inside _collect_candidate_facts to decide which
#       neighbor-facts go into the card's bullets.
#     - Wiki growth selection (wiki_growth.json): currently uses
#       min_degree only — Entity importance is NOT read. This is the
#       Dairy Aisle bug; the planned fix routes selection through
#       effective_importance × degree.
#     - KG visualizer (graph_visualizer/api.py): serialized to the
#       client; the slider on the visualizer page filters node
#       visibility by importance.
#     - Lens layout (me/layout.py): blended with PageRank via
#       LENS_BLEND_IMPORTANCE_WEIGHT × importance + LENS_BLEND_PAGERANK_WEIGHT
#       × pagerank → position on the radial layout.
#     - Context engine convergence (context_activation.py): the
#       "important shared nodes between two seeds" — first the engine
#       finds nodes that BOTH seeds activate (convergence_score), then
#       admits to the wave-1 briefing those whose importance clears
#       CONTEXT_ACTIVATION_THRESHOLD (or whose node_type is
#       Entity/Concept regardless of importance, since types track
#       conceptual significance independently).
#     - State importance (regenerate_state_importance below): the
#       src_imp factor in MAX(src_imp × edge_imp / 10). States inherit
#       importance from their participant Entities through this.
#     - Section tag importance gate (section_tagging.py via
#       queries.max_source_entity_importance): a fact whose strongest
#       owner-Entity has low importance is dropped at tag-write time.
#     - Timeline visual marker (display.importance_marker): ★ / · band.
#     - KG investigator finding priority (kg_investigator/
#       finding_priority.py): which findings to investigate first.
# ════════════════════════════════════════════════════════════════════════════
def regenerate_entity_importance(*, only_unrated: bool = True) -> int:
    """Derive Entity / Concept node importance from edge importance.

    SEMANTIC. The number this writes answers: "how important is this
    Entity / Concept to someone in the graph?" Note: "to SOMEONE", not
    "to Jukka specifically." It's the maximum case across all
    perspectives: if anyone in the graph has a very-important edge
    pointing at this entity, the entity is high-importance.

    Formula:
        Entity.importance = MAX(edge.importance for edge in adjacent_edges)

    Pure max over the incident edges. Both directions count.

    WHY MAX (not sum, not average):
      - Sum rewards fan-out: an entity with 100 mediocre edges would
        outrank an entity with 1 critical edge. Dairy Aisle (15 mediocre
        `located_in` edges) would beat Phil's mother (1 deep `parent_of`
        edge). That's wrong by every intuition that matters.
      - Average penalizes hubs: real high-degree entities (Jukka has
        2200 edges) have a few critical edges and many merely-relevant
        ones; averaging would dilute the signal.
      - Max picks "the strongest reason anyone cares." Anchors on the
        single most-important relationship into the node. Matches the
        intuition: Phil is important because of his close-friendship
        bond with Jukka (rated 8.5), not because of the 50 incidental
        edges from being mentioned in conversations.

    WHY DERIVED, NOT LLM-RATED. The LLM Entity rater (deprecated and
    removed 2026-05-12) rated Emi at 3.0 despite 346 edges + 154
    observations because the prompt categorized her as an "AI tool"
    and the rater anchored on that. The edge graph already encodes
    the truth — and the truth is "Jukka cares deeply about Emi"
    expressed through edges like `Jukka --addressee--> Emi` at 9. The
    rater's job was just to pick a number; the edges had already done
    the harder work. Removing the LLM step removed the source of
    misjudgments and a recurring LLM cost.

    WHAT IT ACCOMPLISHES (downstream consumers):
      - Card-worthiness gate (consumers.is_card_worthy): an Entity
        with high importance gets its own card; low-importance Entities
        live as bullets on other cards.
      - Card fact floor (CARD_FACT_FLOOR in _collect_candidate_facts):
        an Entity in someone else's neighborhood is admitted as a
        candidate fact at importance >= 0.6.
      - Wiki growth selection (currently uses min_degree, but the
        planned replacement reads this value × degree).
      - Lens layout (me/layout.py): blended with PageRank to position
        entities in the 2D radial layout.
      - Timeline marker (display.importance_marker): visual ★ / · in
        the timeline display.
      - State derivation (regenerate_state_importance below): the src
        side of the MAX(src_imp × edge_imp / 10) formula. So this
        function MUST run before that one.

    FAILURE MODES:
      - Fan-out hub artifacts: one detailed chat that mints 15 sibling
        edges all pointing at a hub (Dairy Aisle, Store Perimeter)
        inflates the hub's MAX-adjacent-edge to the max of those 15
        siblings, even though the hub itself is trivia from Jukka's
        perspective. The damping layer (damping.py) is the
        deterministic correction.
      - Sparse Entity bias: an Entity with only one edge has importance
        = that edge's importance. If that edge was rated optimistically,
        the Entity inherits the optimism. Mitigated by multiplying
        adjacent edges into the State derivation below, which doesn't
        propagate this artifact further into the graph.
      - NULL gating: edges without `edge.importance` (still NULL
        because the rater hasn't backfilled them) get excluded.
        Derivation is therefore a lower bound; running this function
        again after edge rater backfill lifts the floor.

    Args:
        only_unrated: when True (default), skip nodes whose
            ``importance`` is non-null. False = re-rate all eligible nodes.

    Returns the number of nodes updated.
    """
    from sqlalchemy import text as sql_text

    started = time.time()

    null_filter = "AND n.importance IS NULL" if only_unrated else ""
    select_sql = f"""
        SELECT n.id,
               MAX(e.importance) AS score
        FROM kg_node_metadata n
        JOIN kg_edge_metadata e
          ON e.source_id = n.id OR e.target_id = n.id
        WHERE n.node_type IN ('Entity', 'Concept')
          AND e.importance IS NOT NULL
          {null_filter}
        GROUP BY n.id
    """

    with get_db_manager().read_session() as session:
        rows = session.execute(sql_text(select_sql)).fetchall()

    updates = [(r[0], float(r[1])) for r in rows if r[1] is not None]
    if not updates:
        logger.info("entity importance: no eligible nodes (only_unrated=%s)", only_unrated)
        return 0

    persisted = 0
    failed = 0
    with get_db_manager().transaction(op="entity_importance_persist") as session:
        for nid, score in updates:
            try:
                session.query(Node).filter(Node.id == nid).update(
                    # updated_at self-reference: importance is a derived
                    # score; bumping updated_at cascades into wiki + card
                    # refreshes on no semantic change.
                    {Node.importance: score, Node.updated_at: Node.updated_at},
                    synchronize_session=False,
                )
                persisted += 1
            except Exception as e:
                logger.warning("entity importance: DB write failed for node %s: %s", nid, e)
                failed += 1
        session.commit()

    elapsed = time.time() - started
    logger.info(
        "entity importance: rated %d Entity/Concept nodes in %.1fs "
        "(%d failures, only_unrated=%s)",
        persisted, elapsed, failed, only_unrated,
    )
    # The lens cache reads node.importance via cache.get_importance_map();
    # without invalidating after a write, lens consumers (me/api.py,
    # me/layout.py, me/pagerank.py) serve stale values until process
    # restart. Local import to avoid circular dependency at module load.
    from app.assistant.importance.cache import invalidate as _invalidate_cache
    _invalidate_cache()
    return persisted


# ════════════════════════════════════════════════════════════════════════════
#   STATE / EVENT / GOAL / PROPERTY IMPORTANCE  —  perspective: inherited
#                                                  from strongest participant
#
#   PERSPECTIVE — proxied through participants. These node types are
#   not "things in themselves" the way an Entity is — they're facts /
#   events / aspirations that ENTITIES participate in. "Phil's phone
#   number is X" is a State that connects Phil (an Entity) to a phone-
#   number Entity. The State's importance is borrowed from how much
#   the participating Entities care about the relationship — which is
#   what edge.importance already captures.
#
#   So a State's importance is not "the user's view of this fact"; it's
#   "the maximum, over all participants, of [how much that participant
#   cares × how heavy this particular edge is for them]." If even one
#   participant has high importance AND a heavy edge into the State,
#   the State inherits that pull. Other low-importance participants
#   can't drag it down.
#
#   COMPOSITION — single-hop chain (Entity → edge → State), with
#   multiplication along the chain and MAX across alternatives:
#
#       State.importance = MAX over connecting edges of
#                          (other_end.importance × edge.importance / 10)
#
#   Walk every edge touching this State; for each edge look at the
#   OTHER end (the Entity, not the State itself), multiply that
#   Entity's importance by the edge's importance, divide by 10 to
#   keep the product in 0-10. Take the maximum.
#
#   Why multiplication along the chain: a strong Entity tied via a
#   weak edge shouldn't pull the State up just because the Entity is
#   important on its own. The product expresses "how much this PATH
#   conveys importance." A weak link kills the contribution; a strong
#   Entity through a strong edge fully transmits.
#
#   Why MAX across alternatives: same fan-out concern as Entity
#   importance. Sum would reward States that touch many low-pull
#   participants; max picks the single strongest path.
#
#   DEPENDENCY ORDER — this function MUST run AFTER
#   regenerate_entity_importance because the src.importance factor is
#   the Entity-side value. If Entity importance is NULL the join in
#   the SQL drops the edge and the State gets no rating that pass.
#   The routine pipeline is sequenced: edge rater → entity derivation
#   → state derivation, for exactly this reason.
#
#   CONSUMERS — who reads State / Event / Goal / Property importance:
#     - State decay priority (step_state_decay.py): higher importance
#       States get evaluated first when the decay routine sweeps for
#       stale facts to close. Below a low floor decay runs quietly
#       in the background.
#     - Card fact admission (_collect_candidate_facts): each State
#       connected to the card's subject Entity becomes a candidate
#       fact; State.importance is the value compared against
#       CARD_FACT_FLOOR.
#     - Section-tagger admission gate (section_tagging.py): a fact
#       whose own importance is below the tagger's threshold is
#       skipped at tag time.
#     - Missing-dates investigator priority (step_missing_dates_scan):
#       date-less States/Events with higher importance get
#       investigated first.
#     - Timeline gap question filter (timeline_gaps.py): only States
#       at TIMELINE_CONFIRMATION_FLOOR or above produce user-facing
#       confirmation prompts; low-importance gaps stay silent.
#     - Timeline display marker (display.importance_marker): ★ / · in
#       timeline rendering for visual prominence.
#     - KG visualizer (graph_visualizer/api.py): serialized to the
#       client alongside Entity importance for the visibility slider.
#     - Goal dormancy sweep: Goal-specific consumer (lifecycle
#       lives at `consumers.is_goal_active`, currently inline in
#       goal_dormancy_sweep).
# ════════════════════════════════════════════════════════════════════════════
def regenerate_state_importance(*, only_unrated: bool = True) -> int:
    """Fill State / Event / Goal / Property node importance from edge importance.

    SEMANTIC. The number this writes answers: "how important is this
    fact / event / goal to ANY entity that participates in it?" States
    don't exist in isolation — "Phil's phone number is X" is a State
    that participates in the relationship between Phil and that phone
    number. The State's importance is inherited from the most-important
    participant, scaled by how much THAT participant cares about
    the relationship.

    Formula:
        State.importance = MAX(src.importance × edge.importance / 10)
                           over all edges touching this State

    where:
      - `src` is the entity on the OTHER end of the edge (the one
        anchoring the relationship, not the State itself)
      - `edge` is the edge connecting `src` to the State
      - Both factors are 0-10, the /10 keeps the product in 0-10.

    WHY MULTIPLICATION (not max of one component, not addition):
      - A high-importance Entity (Phil at 8.5) tied via an
        unimportant edge (rated 1) shouldn't pull the State up. The
        State derives importance from the LINK to Phil, not from
        Phil's existence. Multiplication zeroes out un-pulled
        connections.
      - A low-importance Entity tied via a critical edge (Phil's
        roommate at 2.5, has-state to "Roommate Connection" at 7)
        should still register meaningful importance. Addition can't
        express this — it would over-credit the unimportant entity.
      - Product captures "how much this side of the graph CARES" as
        the magnitude of the relationship, not the magnitude of either
        endpoint alone.

    WHY MAX OVER EDGES (not sum):
      - Same fan-out concern as Entity importance: summing across edges
        rewards States that touch many participants regardless of
        whether those participants care. MAX picks the single strongest
        participant-edge combination.
      - Matches "what's the strongest reason this fact matters?"

    WHY DERIVED, NOT LLM-RATED. State/Event/Goal/Property are connective
    tissue — they exist because two Entity-side things relate. They
    don't have their own evaluative standing the way an Entity does.
    A State's importance is fully a function of who participates and
    how much they care. An LLM rating layer on top would just be
    re-deriving the same signal less directly.

    DEPENDENCY ORDER: this function MUST run AFTER
    regenerate_entity_importance — `src.importance` is what feeds the
    multiplication, and if entity importances haven't been computed
    yet, src.importance is NULL and the join drops the edge.

    WHAT IT ACCOMPLISHES (downstream consumers):
      - State-decay priority (step_state_decay.py): which stale States
        to close first. Higher importance = evaluate first.
      - Card fact floor (CARD_FACT_FLOOR): when an entity's card builds,
        each connecting State is admitted as a candidate fact based
        on its importance.
      - Section-tag admission gate: section_tagger uses
        max_source_entity_importance — but for State nodes whose
        source-entity importance isn't yet set, falls back to
        max_adjacent_edge / 10 (deliberately the SAME scale as
        State.importance derivation so the comparison is fair).
      - Missing-dates investigator priority (step_missing_dates_scan):
        which date-less States to investigate first.
      - Timeline marker (display.importance_marker): visual prominence
        of State/Event entries in timeline renderings.

    FAILURE MODES:
      - Stale State after Entity importance changes: this function
        depends on Entity importance being current. If Entity importance
        is refreshed but State importance isn't re-derived, States
        carry stale values. The routine_functions pipeline runs the
        functions in order (edge → entity → state) precisely for this.
      - Property nodes that are subject-scoped: Properties exist per-
        subject ("Date of Birth" for Jukka is distinct from "Date of
        Birth" for Jouko, per [[project_property_subject_scoped]]). The
        derivation works correctly because the subject's edge is the
        only or strongest tie; but if a Property accidentally has
        cross-subject edges from a bad merge, importance gets confused.
      - NULL Entity importance: skipped via the `src.importance IS NOT
        NULL` filter. State gets no rating until its source Entity is
        rated. Acceptable; the next pipeline pass picks them up.

    Single SQL UPDATE per node; preserves ``updated_at`` to avoid
    cascading wiki + entity-card refreshes on score-only changes (per
    the same protective pattern as step_pagerank).

    Args:
        only_unrated: when True (default), skip nodes whose
            ``importance`` is non-null. False = re-rate all eligible nodes.

    Returns the number of nodes updated.
    """
    from sqlalchemy import text as sql_text

    started = time.time()

    null_filter = "AND n.importance IS NULL" if only_unrated else ""
    select_sql = f"""
        SELECT n.id,
               MAX(src.importance * e.importance / 10.0) AS score
        FROM kg_node_metadata n
        JOIN kg_edge_metadata e
          ON e.source_id = n.id OR e.target_id = n.id
        JOIN kg_node_metadata src
          ON src.id = CASE
                        WHEN e.source_id = n.id THEN e.target_id
                        ELSE e.source_id
                      END
        WHERE n.node_type IN ('State', 'Event', 'Goal', 'Property')
          AND e.importance IS NOT NULL
          AND src.importance IS NOT NULL
          {null_filter}
        GROUP BY n.id
    """

    with get_db_manager().read_session() as session:
        rows = session.execute(sql_text(select_sql)).fetchall()

    updates = [(r[0], float(r[1])) for r in rows if r[1] is not None]
    if not updates:
        logger.info("state importance: no eligible nodes (only_unrated=%s)", only_unrated)
        return 0

    persisted = 0
    failed = 0
    with get_db_manager().transaction(op="state_importance_persist") as session:
        for nid, score in updates:
            try:
                session.query(Node).filter(Node.id == nid).update(
                    {Node.importance: score, Node.updated_at: Node.updated_at},
                    synchronize_session=False,
                )
                persisted += 1
            except Exception as e:
                logger.warning("state importance: DB write failed for node %s: %s", nid, e)
                failed += 1
        session.commit()

    elapsed = time.time() - started
    logger.info(
        "state importance: rated %d State/Event/Goal/Property nodes in %.1fs "
        "(%d failures, only_unrated=%s)",
        persisted, elapsed, failed, only_unrated,
    )
    # Drop lens cache; see comment in regenerate_entity_importance.
    from app.assistant.importance.cache import invalidate as _invalidate_cache
    _invalidate_cache()
    return persisted


if __name__ == "__main__":  # pragma: no cover
    import app.assistant.tests.test_setup  # noqa: F401
    # Entity / Concept derived from edge importance (max adjacent).
    regenerate_entity_importance(only_unrated=False)
    # State / Event / Goal / Property derived via max(src_imp × edge_imp / 10).
    regenerate_state_importance(only_unrated=False)
