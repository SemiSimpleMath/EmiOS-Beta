"""Entity Cards v2 builder.

One entry point:

    build_card(entity_node_id)
        Full rebuild for one entity. Walks the KG neighborhood, applies
        the NOW filter, runs section_tagger to classify each surviving
        fact, runs bullet_renderer per fact, persists sections + bullets.

A failed call (LLM, DB) raises out of build_card before the persist phase:
nothing is written and an existing card stays stale for the next run. A
well-formed but incomplete answer gets one targeted correction, then raises
(owner, 2026-10-05: any failed call in the KG stops processing).

Design: see docs/architecture/12_ENTITY_CARDS_V2_DESIGN.md.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.assistant.entity_management.entity_card_v2 import (
    EntityCardV2, EntityCardSection, EntityCardBullet,
    EntityCardBulletSourceNode,
    section_template_for, bullet_section_names, is_now_admissible,
    bullet_cap_for_section, RENDERER_CHUNK_SIZE,
    BULLETS, ALIAS, SUMMARY, LEVEL_0,
)
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import utc_now
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


# Selection policy for facts reaching the renderer/distiller:
#
# A fact is kept iff EITHER its importance is at or above CARD_FACT_FLOOR,
# OR the section_tagger has marked its source node as `contact` (phone /
# email / address / handle). Contact info is load-bearing for downstream
# agents regardless of the rater's score — the source-perspective edge
# rater systematically undervalues "X's phone number" because it isn't
# important to X, it's important to whoever wants to reach X. Without
# the carve-out, contact States routinely lose the importance cut on
# any moderately-connected person and the card never gets a contact
# section. We also require the source node to still be NOW-admissible,
# so a replaced phone number doesn't resurface.
#
# Safety net: if floor + contact carve-out yields fewer than
# CARD_FACT_MIN_KEEP facts, top up by taking the next most-important
# facts until we reach it. This prevents sparse entities (a person
# mentioned once or twice, with all-low-importance edges) from being
# starved into an empty card.
from app.assistant.importance.consumers import (
    CARD_FACT_FLOOR,
    CARD_FACT_MIN_KEEP,
)


# A bullet may come from multiple source facts. The grouping policy:
# we let section_tagger emit one tag per RAW fact (a fact = one
# connected node + its connecting edge). The bullet_renderer can
# combine multiple facts into one bullet OR produce one bullet per
# fact. v1: one bullet per fact (simpler). Later v1.1: cluster facts
# in the same section by shared subject for combined bullets.


# --- Public entry points ----------------------------------------------------

def build_card(entity_node_id: str, *, activate_new: bool = True) -> Dict[str, Any]:
    """Full rebuild for one entity. Idempotent.

    `activate_new=False` writes a card that does not exist yet as inactive:
    the nightly refresh activates it only after card_critic judges it.

    Walks the KG neighborhood, applies the NOW filter, runs section_tagger
    to classify facts, runs bullet_renderer (chunked) per section, runs
    section_distiller when any section exceeds its cap, builds deterministic
    alias + contact sections, runs summary_writer for the top of the card,
    derives level_0 from the summary, and atomically persists. Existing
    sections are wiped and rewritten — incremental refresh has a separate
    entry point.

    Session discipline: DB reads run in short read_session scopes, every
    LLM call runs with no connection open, and all writes land in one
    short db_manager transaction at the end (drop-marks + card together).

    Returns: summary dict with status, sections, and bullets counts.
    """
    db_manager = get_db_manager()

    # Read phase — entity + gates. The session closes before any LLM work;
    # `entity` is used below as a detached instance (plain column reads).
    with db_manager.read_session() as session:
        entity = session.get(Node, entity_node_id)
        if entity is None:
            return {'status': 'error', 'reason': f'node {entity_node_id} not found'}

        # 0a. The user's own entity has no card. Everything Emi needs to
        # know about Jukka lives in resource_user_data, user_bio_context,
        # health summaries, etc. — those flow into prompts directly. A
        # card here just duplicates context (and is also the heaviest
        # one to build).
        if _is_user_self_entity(entity):
            logger.info(
                "[entity_cards_v2] %s: user-self entity — no card built",
                entity.label,
            )
            return {
                'status': 'skipped',
                'reason': 'user-self entity has no card (data lives in resource_user_data)',
            }

        # 0. Card-worthiness gate. Routed through the canonical gate at
        # `consumers.is_card_worthy`. Policy: pass if any of
        #   - locked_by_user_at IS NOT NULL                  (user-pinned)
        #   - observation_count >= 2                         (re-observed)
        #   - observation_count == 1 AND degree >= 10        (one deep window)
        #   - effective_importance >= CARD_HIGH_IMPORTANCE_FLOOR
        #                                                    (graph says defining)
        # We compute degree here once and pass it in so is_card_worthy
        # doesn't open its own session.
        from app.assistant.importance.consumers import is_card_worthy
        degree = session.query(Edge).filter(
            (Edge.source_id == entity.id) | (Edge.target_id == entity.id)
        ).count()
        if not is_card_worthy(entity, degree=degree):
            obs = entity.observation_count or 0
            logger.info(
                "[entity_cards_v2] %s: not card-worthy "
                "(obs=%d degree=%d importance=%s) — skipping",
                entity.label, obs, degree, entity.importance,
            )
            return {
                'status': 'skipped',
                'reason': (
                    f'not card-worthy: obs={obs} degree={degree} '
                    f'importance={entity.importance}'
                ),
            }

    # 1. Collect candidate facts (NOW-filtered neighborhood). Manages its
    # own short read sessions; the nano prefilter's LLM calls run with no
    # connection open.
    facts = _collect_candidate_facts(entity)
    if not facts:
        logger.info("[entity_cards_v2] no candidate facts for %s — skipping", entity.label)
        return {'status': 'skipped', 'reason': 'no candidate facts after NOW filter'}

    # 2. Tag each fact into a BULLET section (or 'reject').
    # Prefer persisted tags from kg_node_section_tag (written at promotion
    # time by section_tagging.tag_nodes_by_id). Run the live tagger only
    # when NO facts have persisted tags — that's transition-period
    # behavior; once the backfill lands, the live path becomes dead code.
    section_template = section_template_for(entity.node_type, entity.category)
    bullet_sections = bullet_section_names(section_template)
    with db_manager.read_session() as session:
        tagged = _tags_from_persisted(session, facts, bullet_sections)
    if not tagged:
        logger.info(
            "[entity_cards_v2] no persisted tags for %s — running live section_tagger",
            entity.label,
        )
        tagged = _run_section_tagger(entity, facts, bullet_sections)

    # 3. Group facts by section. `reject` and hallucinated section names
    # are NOT silently dropped — the fact already passed the NOW filter
    # and the top-K cut, so "should it exist on the card" was already
    # decided. The section_tagger's only job is
    # bucket selection. Any fact whose chosen section is invalid or
    # `reject` is routed to `general_facts`, which exists on every
    # template (see SECTION_TEMPLATES). This was Issue surfaced 2026-05-12
    # when Place entities (Espoo, Finland, California, ...) hit the
    # `_default` template, had no semantic fit, and the tagger rejected
    # all their edges — producing empty cards for real entities.
    rerouted = 0
    by_section: Dict[str, List[Dict[str, Any]]] = {}
    for tag in tagged:
        if tag['section'] == 'reject' or tag['section'] not in bullet_sections:
            if 'general_facts' in bullet_sections:
                rerouted += 1
                by_section.setdefault('general_facts', []).append(
                    {**tag, 'section': 'general_facts'}
                )
            continue
        by_section.setdefault(tag['section'], []).append(tag)
    if rerouted:
        logger.info(
            "[entity_cards_v2] %s: %d fact(s) rerouted from reject/hallucinated → general_facts",
            entity.label, rerouted,
        )

    # 4. Render bullets per section. Three-stage pipeline:
    #    a. Chunk the renderer input if section has > RENDERER_CHUNK_SIZE
    #       facts (so the LLM never fuses over too much at once).
    #    b. Run bullet_renderer on each chunk; concatenate results.
    #    c. If concatenated bullets exceed the section's cap, run
    #       section_distiller to do the final salience cut.
    # The renderer's job = fuse near-duplicates. The distiller's job =
    # pick the most identity-defining bullets and drop the rest.
    fact_by_id = {f['fact_id']: f for f in facts}
    section_bullets: Dict[str, List[Dict[str, Any]]] = {}
    # (section_name, tagged facts, persisted bullets) per rendered section —
    # drop-marks are computed now but written in the single write phase below.
    pending_drops: List[tuple] = []
    for section_name, tags in by_section.items():
        section_facts = [
            fact_by_id[t['fact_id']] for t in tags
            if t['fact_id'] in fact_by_id
        ]
        if not section_facts:
            continue

        # Stage 2: chunked rendering. A failed chunk ABORTS the whole build:
        # swallowing it used to let the persist phase delete the card's existing
        # sections, write the survivors (possibly nothing), stamp last_built_at,
        # and mark the failed chunk's facts as renderer-REJECTED — an LLM outage
        # recorded as a fresh successful rebuild. Raising leaves the old card
        # intact and still stale, so the next refresh retries it.
        rendered_bullets: List[Dict[str, Any]] = []
        for chunk_start in range(0, len(section_facts), RENDERER_CHUNK_SIZE):
            chunk = section_facts[chunk_start:chunk_start + RENDERER_CHUNK_SIZE]
            rendered_bullets.extend(_run_bullet_renderer(entity, section_name, chunk))

        if not rendered_bullets:
            continue

        # Materialize rendered bullets with id + source-id provenance, so
        # distiller can pick by id and we can union the source nodes/edges.
        renderer_bullets: List[Dict[str, Any]] = []
        for i, rb in enumerate(rendered_bullets):
            source_facts = [fact_by_id[fid] for fid in rb['source_fact_ids']]
            node_ids: List[str] = []
            edge_ids: List[str] = []
            for sf in source_facts:
                for nid in sf['source_node_ids']:
                    if nid not in node_ids:
                        node_ids.append(nid)
                for eid in sf['source_edge_ids']:
                    if eid not in edge_ids:
                        edge_ids.append(eid)
            tiers = [sf.get('confidence_tier', 'provisional') for sf in source_facts]
            tier_rank = {'inferred': 0, 'provisional': 1, 'confirmed': 2, 'axiom': 3}
            worst_tier = min(tiers, key=lambda t: tier_rank.get(t, 1))
            renderer_bullets.append({
                'bullet_id': f'b{i+1}',
                'bullet_text': rb['bullet_text'],
                'source_node_ids': node_ids,
                'source_edge_ids': edge_ids,
                'confidence_tier': worst_tier,
                'last_said': _latest([sf.get('said') for sf in source_facts]),
            })

        # Stage 3: distillation — only when section exceeds its cap. A failed
        # distiller aborts the build: keeping the uncapped renderer output used
        # to persist an over-cap section as a fresh build.
        cap = bullet_cap_for_section(section_name)
        if len(renderer_bullets) > cap:
            distilled = _run_section_distiller(entity, section_name, renderer_bullets, cap)
        else:
            distilled = None

        if distilled is not None:
            # Distiller picked + possibly fused; map source_bullet_ids → renderer bullets
            rb_by_id = {b['bullet_id']: b for b in renderer_bullets}
            persisted = []
            for db in distilled:
                source_rbs = [rb_by_id[bid] for bid in db['source_bullet_ids']]
                node_ids = []
                edge_ids = []
                tiers = []
                for srb in source_rbs:
                    for nid in srb['source_node_ids']:
                        if nid not in node_ids:
                            node_ids.append(nid)
                    for eid in srb['source_edge_ids']:
                        if eid not in edge_ids:
                            edge_ids.append(eid)
                    tiers.append(srb['confidence_tier'])
                tier_rank = {'inferred': 0, 'provisional': 1, 'confirmed': 2, 'axiom': 3}
                worst_tier = min(tiers, key=lambda t: tier_rank.get(t, 1))
                persisted.append({
                    'bullet_text': db['bullet_text'],
                    'source_node_ids': node_ids,
                    'source_edge_ids': edge_ids,
                    'confidence_tier': worst_tier,
                    'last_said': max((srb['last_said'] for srb in source_rbs if srb.get('last_said')), default=None),
                })
        else:
            # No distillation; drop bullet_id (it was only for distiller threading)
            persisted = [
                {k: v for k, v in rb.items() if k != 'bullet_id'}
                for rb in renderer_bullets
            ]

        if persisted:
            section_bullets[section_name] = persisted

        # Try-and-mark: any input fact whose primary node did NOT end up
        # in this section's output bullets was considered and rejected by
        # the renderer/distiller. The rejection is persisted in the write
        # phase below, so the next refresh doesn't re-pay LLM cost to
        # re-discover it. Reconsideration is automatic when (a) node content
        # changes (hash mismatch) or (b) CARD_BUILDER_VERSION is bumped.
        pending_drops.append((section_name, tags, persisted))

    # 5. Build deterministic sections (alias only; contact is now
    # LLM-tagged via section_tagger like every other BULLETS section).
    section_intros: Dict[str, str] = {}
    alias_text = _build_alias_section(entity)
    if alias_text:
        section_intros[ALIAS] = alias_text

    # 6. Build summary (LLM) AFTER bullets — uses bullets + alias, NOT contact.
    # A card with no summary is a failed build: _build_summary_section raises.
    summary_text = _build_summary_section(entity, section_bullets, alias_text)
    section_intros[SUMMARY] = summary_text

    # 7. Build level_0 (deterministic head, depends on summary)
    section_intros[LEVEL_0] = _build_level_0(summary_text)

    # 8. Persist — the build's only transaction. All LLM work happened above
    # with no session open; this block holds the global writer slot and must
    # stay milliseconds (db_manager warns past 5s).
    with db_manager.transaction(op="entity_cards.build_card") as session:
        for section_name, tags, persisted in pending_drops:
            _mark_section_drops(session, tags, persisted, section_name, fact_by_id)
        result = _persist_card(
            session, entity, section_template, section_bullets, section_intros,
            activate_new=activate_new,
        )
    return {'status': 'ok', **result}


# --- Implementation: candidate fact collection ------------------------------

# ── Predicates we walk through at 2-hop ────────────────────────────────────
# State/Event/Goal/Concept nodes are the temporal/conceptual bridges in this
# graph (per the temporal-life-graph rule: connections between Entities route
# through one of these). When the card subject has, e.g., a "Dogs" entity
# whose connected Entities (Bonnie + Clyde) live behind a 1-hop Ownership
# State, the 2-hop walk through that State is what surfaces the names without
# reading Node.description (which is wiki-derived and would collapse the
# cross-check between card and wiki projections).
_BRIDGE_NODE_TYPES = {"State", "Event", "Goal", "Concept"}

# Max Entity endpoints to surface per 1-hop bridge (avoid hub explosion when
# a State/Event has many participants). Most bridges carry a handful; this
# only bites on hub bridges like "Conversation" or "Family Time" that link
# to lots of people. The 1-hop importance sort still applies to bridge
# selection, so high-importance bridges are visited first.
_HOP2_MAX_ENDPOINTS_PER_BRIDGE = 5

# ── Nano prefilter (wiki parity) ───────────────────────────────────────────
# Mirrors description_creator._prefilter_edges. Entities whose collected
# fact pool exceeds _CARD_PREFILTER_THRESHOLD are run through the
# kg_maintenance::edge_filter nano agent in batches of
# _CARD_PREFILTER_BATCH_SIZE; nano picks the most informative facts and
# the rest are dropped before the importance sort + floor cut. Same
# thresholds as the wiki path so behaviour is symmetric on the hub
# entities (Jukka, House, Family, etc.) that have hundreds of edges.
_CARD_PREFILTER_THRESHOLD = 60
_CARD_PREFILTER_BATCH_SIZE = 200


def _chunk(lst: List[Any], size: int) -> List[List[Any]]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]


def _format_fact_oneliner(fact: Dict[str, Any], index: int) -> str:
    """One-line descriptor of a fact for the nano prefilter prompt.

    Mirrors description_creator._format_edge_oneliner but adapted to the
    card-fact dict shape. NOT sliced — sentences pass full per the no-
    slicing rule. The nano agent sees one fact per line; the prompt-side
    chunking in _prefilter_facts keeps each batch bounded.
    """
    direction = (fact.get("direction") or "?").upper()
    edge_type = fact.get("edge_type") or "unknown"
    label = fact.get("object_label") or "?"
    object_type = fact.get("object_type") or "?"
    hop = fact.get("hop") or 1
    hop_tag = "[H2] " if hop > 1 else ""
    line = f"{index}. {hop_tag}{direction}: {edge_type} -> {label} ({object_type})"
    sentence = (fact.get("edge_sentence") or "").strip()
    if sentence:
        line += f' | "{sentence}"'
    return line


def _prefilter_facts(
    facts: List[Dict[str, Any]],
    entity_label: str,
    entity_type: str,
) -> List[Dict[str, Any]]:
    """Nano-LLM prefilter for high-degree entity fact pools.

    Wiki parity (description_creator._prefilter_edges). Below threshold,
    pass through unchanged. Above threshold, batch the facts and ask
    kg_maintenance::edge_filter to pick the informative ones. A failed
    batch raises out of the build (until 2026-10-05 it kept every fact and
    the card was persisted as a fresh build); a batch answer with no or
    out-of-range keep_indices gets one correction, then raises.
    """
    if len(facts) <= _CARD_PREFILTER_THRESHOLD:
        return facts

    logger.info(
        "[card_prefilter] %s has %d facts — running nano prefilter",
        entity_label, len(facts),
    )
    from app.assistant.ServiceLocator.service_locator import DI
    agent = DI.agent_factory.create_agent("kg_maintenance::edge_filter")
    if agent is None:
        raise RuntimeError("agent kg_maintenance::edge_filter not available")

    keep_global: set[int] = set()
    index_batches = _chunk(list(range(len(facts))), _CARD_PREFILTER_BATCH_SIZE)
    for batch_num, global_indices in enumerate(index_batches, 1):
        lines = [
            _format_fact_oneliner(facts[global_i], local_i)
            for local_i, global_i in enumerate(global_indices, 1)
        ]
        data = _ask_with_one_correction(
            agent, f"edge_filter batch {batch_num} for {entity_label!r}",
            {
                "node_label": entity_label,
                "node_type": entity_type,
                "edge_list": "\n".join(lines),
            },
            lambda d: _keep_indices_problems(d, len(global_indices)),
        )
        for local_idx in data["keep_indices"]:
            keep_global.add(global_indices[local_idx - 1])

    filtered = [facts[i] for i in sorted(keep_global)]
    logger.info(
        "[card_prefilter] %s: %d -> %d facts (%.0f%% reduction)",
        entity_label, len(facts), len(filtered),
        (1 - len(filtered) / len(facts)) * 100 if facts else 0,
    )
    return filtered


def _keep_indices_problems(data: Dict[str, Any], n_lines: int) -> List[str]:
    keep = data.get("keep_indices") or []
    if not keep:
        return [f"keep_indices is empty: list the numbers of the edges to keep (1 to {n_lines})"]
    stray = [i for i in keep if not isinstance(i, int) or not 1 <= i <= n_lines]
    if stray:
        return [f"keep_indices {stray} are not edge numbers in the list (1 to {n_lines})"]
    return []


def _ask(agent: Any, what: str, agent_input: Dict[str, Any]) -> Dict[str, Any]:
    """One agent call; an answer without structured data raises."""
    result = agent.action_handler(Message(agent_input=agent_input, scope_context=_scope()))
    data = getattr(result, 'data', None)
    if hasattr(data, 'model_dump'):
        data = data.model_dump()
    if not isinstance(data, dict):
        raise RuntimeError(f"{what}: the agent returned no structured answer ({type(data).__name__})")
    return data


def _ask_with_one_correction(
    agent: Any, what: str, agent_input: Dict[str, Any], problems_of,
) -> Dict[str, Any]:
    """A well-formed but incomplete answer gets ONE targeted correction (the
    agent's `correction` prompt slot); still incomplete -> raise. `correction`
    is always passed: agent_input lands on the agent's blackboard, so a
    reused agent would otherwise carry the previous call's correction."""
    data = _ask(agent, what, {**agent_input, "correction": ""})
    problems = problems_of(data)
    if not problems:
        return data
    logger.warning("[entity_cards_v2] %s incomplete, one correction: %s", what, "; ".join(problems))
    data = _ask(agent, what, {**agent_input, "correction": "; ".join(problems)})
    problems = problems_of(data)
    if problems:
        raise RuntimeError(f"{what}: still incomplete after one correction: {'; '.join(problems)}")
    return data


def _is_user_self_entity(entity: Node) -> bool:
    """True when this Entity is the user themselves.

    Used to short-circuit card building — the user's data lives in
    resource_user_data, user_bio_context, etc., and is injected into
    prompts through other channels. A card here would just duplicate.

    Matches the label, case-insensitively, against the canonical primary user name (the same name the nightly
    refresh gates on). Raises when that name is not configured. Until 2026-10-05 this read resource_user_data with a
    positional get_resource call that raised TypeError on every build; the error was logged at DEBUG and the check
    returned False.
    """
    if (entity.node_type or "") != "Entity":
        return False
    from app.assistant.utils.identity_names import get_required_primary_user_name
    return (entity.label or "").strip().lower() == get_required_primary_user_name().strip().lower()


def _collect_candidate_facts(entity: Node) -> List[Dict[str, Any]]:
    """Walk the entity's edges, build a list of NOW-admissible facts.

    A "fact" = one connected node (or its connecting edge) that survives
    the NOW filter. Returns dicts ready to send to section_tagger.

    After collection, the list is sorted by importance descending and
    truncated to CARD_FACT_TOP_K. On sparse entities the truncation is a
    no-op (count < K); on hubs it keeps the most-important slice.

    DB reads run in short read_session scopes — the KG walk's session is
    closed before the nano prefilter's LLM calls run.
    """
    with get_db_manager().read_session() as session:
        facts = _walk_candidate_facts(session, entity)

    # Wiki-parity nano prefilter. For hub entities the 1-hop + 2-hop walks
    # can produce hundreds of facts; running each through section_tagger +
    # bullet_renderer is slow and dilutes the signal. The same nano agent
    # the wiki's description_creator uses (kg_maintenance::edge_filter)
    # picks the informative subset; the existing importance floor + min-
    # keep top-up then operate on that smaller pool. See _prefilter_facts.
    facts = _prefilter_facts(facts, entity.label, entity.node_type)

    # Sort by importance descending so downstream rendering chunks see the
    # high-importance facts FIRST. If the section has 30 input facts and the
    # renderer chunks them in groups of 10, the top-importance bullets are
    # guaranteed in the first chunk. NULL importance sorts at the end.
    def _imp(f: Dict[str, Any]) -> float:
        # Scale-aware default. When node_importance is NULL (rater hasn't
        # run yet), use edge_importance / 10 so a missing-rating fact
        # lands on the same 0-10 scale as State.importance (which is
        # derived as `max(src_imp × edge_imp / 10)`). Without the /10
        # rescale, unrated facts scored 5 would dominate properly-rated
        # States at 0.5–3, distorting card fact ordering.
        ni = f.get('node_importance')
        if ni is not None:
            return float(ni)
        ei = f.get('edge_importance')
        if ei is not None:
            return float(ei) / 10.0
        return 0
    facts.sort(key=lambda f: -_imp(f))

    # Apply the selection policy (see CARD_FACT_FLOOR comment).
    with get_db_manager().read_session() as session:
        contact_node_ids = _contact_tagged_node_ids(
            session,
            [f['source_node_ids'][0] for f in facts if f.get('source_node_ids')],
        )

    def _is_contact(f: Dict[str, Any]) -> bool:
        sids = f.get('source_node_ids') or []
        return bool(sids) and sids[0] in contact_node_ids

    kept = [f for f in facts if _imp(f) >= CARD_FACT_FLOOR or _is_contact(f)]

    if len(kept) < CARD_FACT_MIN_KEEP:
        kept_fact_ids = {f['fact_id'] for f in kept}
        for f in facts:
            if f['fact_id'] in kept_fact_ids:
                continue
            kept.append(f)
            if len(kept) >= CARD_FACT_MIN_KEEP:
                break

    if len(kept) < len(facts):
        logger.info(
            "[entity_cards_v2] %s: %d facts collected, kept %d (floor=%.2f, contact=%d, min=%d)",
            entity.label, len(facts), len(kept),
            CARD_FACT_FLOOR, len(contact_node_ids), CARD_FACT_MIN_KEEP,
        )

    return kept


def _walk_candidate_facts(session: Session, entity: Node) -> List[Dict[str, Any]]:
    """1-hop + 2-hop KG walk. Returns raw NOW-admissible fact dicts,
    unfiltered and unsorted — selection happens in _collect_candidate_facts.
    """
    facts: List[Dict[str, Any]] = []
    fid_counter = 0

    def _next_fid() -> str:
        nonlocal fid_counter
        fid_counter += 1
        return f"f{fid_counter}"

    # Outgoing + incoming edges
    edges = session.query(Edge).filter(
        (Edge.source_id == entity.id) | (Edge.target_id == entity.id)
    ).all()

    for edge in edges:
        is_outgoing = edge.source_id == entity.id
        other_id = edge.target_id if is_outgoing else edge.source_id
        other = session.get(Node, other_id)
        if other is None:
            continue

        # NOW filter on the OTHER side (the connected node)
        admissible = is_now_admissible(
            other.node_type, other.end_date, other.category,
            other.goal_status, other.locked_by_user_at,
            valid_currently=getattr(other, "valid_currently", None),
        )
        if not admissible:
            continue

        node_imp = other.importance
        edge_imp = edge.importance

        # Build a fact descriptor
        facts.append({
            'fact_id': _next_fid(),
            'source_node_ids': [other.id],
            'source_edge_ids': [edge.id],
            'subject_label': entity.label,
            'object_label': other.label,
            'object_type': other.node_type,
            'object_category': other.category,
            'edge_type': edge.relationship_type,
            'edge_sentence': edge.sentence,
            'object_sentence': other.original_sentence,
            'object_description': other.description,
            'direction': 'outgoing' if is_outgoing else 'incoming',
            'confidence_tier': other.confidence_tier or 'provisional',
            # Importance signal carried through so downstream renderer +
            # distiller can sort/prioritize high-importance facts when
            # cutting to bullet cap.
            'node_importance': float(node_imp) if node_imp is not None else None,
            'edge_importance': float(edge_imp) if edge_imp is not None else None,
            'hop': 1,
            'temporal': {
                'start_date': str(other.start_date) if other.start_date else None,
                'end_date': str(other.end_date) if other.end_date else None,
                'start_date_prose': other.start_date_prose,
                'end_date_prose': other.end_date_prose,
                'valid_during': other.valid_during,
            },
        })

    # ── 2-hop walk through State/Event/Goal bridges ──
    # Per the temporal-life-graph rule, Entity↔Entity connections route
    # through a State/Event/Goal (or Concept). The card builder's 1-hop
    # walk above sees those bridges as direct neighbors but doesn't follow
    # them to their other Entity endpoints. That's why "Dogs" doesn't
    # name Bonnie + Clyde at 1-hop — they live behind an Ownership State.
    #
    # The wiki's description_creator effectively gets the names because
    # it reads each connected node's description (which is itself wiki-
    # generated and names them). The card MUST NOT do that — it would
    # collapse the card/wiki cross-check. Instead, we walk the structure
    # ourselves and pick up the Entity endpoints behind each bridge.
    #
    # Capped per bridge so a high-fanout State (e.g., a recurring event
    # with many participants) doesn't dump all of them into the pool.
    bridge_neighbor_ids = {
        (f['source_node_ids'][0], f['source_edge_ids'][0])
        for f in facts
        if (f.get('object_type') or '') in _BRIDGE_NODE_TYPES
    }
    seen_2hop_entity_ids: set[str] = set()
    for (bridge_id, bridge_via_edge_id) in bridge_neighbor_ids:
        bridge_node = session.get(Node, bridge_id)
        if bridge_node is None:
            continue
        bridge_edges = session.query(Edge).filter(
            (Edge.source_id == bridge_id) | (Edge.target_id == bridge_id)
        ).all()
        per_bridge = 0
        for e2 in bridge_edges:
            if str(e2.id) == bridge_via_edge_id:
                continue
            other2_id = e2.target_id if e2.source_id == bridge_id else e2.source_id
            if other2_id == entity.id:
                continue
            other2 = session.get(Node, other2_id)
            if other2 is None:
                continue
            # Only collect Entity endpoints at hop 2. State→State chains
            # would pull noise; the goal is "what Entities are on the
            # other side of this bridge."
            if (other2.node_type or '') != "Entity":
                continue
            # Dedup across bridges (same Entity reachable via multiple
            # bridges should only count once).
            if str(other2.id) in seen_2hop_entity_ids:
                continue
            # NOW filter — if the 2-hop Entity itself is closed/archived,
            # skip.
            if not is_now_admissible(
                other2.node_type, other2.end_date, other2.category,
                other2.goal_status, other2.locked_by_user_at,
                valid_currently=getattr(other2, "valid_currently", None),
            ):
                continue

            # Compose the 2-hop fact. edge_sentence narrates the path so
            # bullet_renderer has full context. object_description is
            # NOT included (would be reading wiki output per the cross-
            # check principle).
            composed_sentence = (
                f"Via {bridge_node.node_type} '{bridge_node.label}': "
                f"{e2.sentence or ''}"
            ).strip()
            facts.append({
                'fact_id': _next_fid(),
                'source_node_ids': [other2.id, bridge_node.id],
                'source_edge_ids': [e2.id, bridge_via_edge_id],
                'subject_label': entity.label,
                'object_label': other2.label,
                'object_type': other2.node_type,
                'object_category': other2.category,
                'edge_type': e2.relationship_type,
                'edge_sentence': composed_sentence,
                'object_sentence': other2.original_sentence,
                'object_description': None,
                'direction': '2hop',
                'confidence_tier': other2.confidence_tier or 'provisional',
                # 2-hop facts use the 1-hop bridge's importance × the 2nd-
                # leg edge's importance / 10 so the score remains on the
                # 0-10 scale and dampens compared to direct 1-hop facts.
                'node_importance': float(other2.importance) if other2.importance is not None else None,
                'edge_importance': (
                    float(e2.importance) if e2.importance is not None else None
                ),
                'hop': 2,
                'via_bridge_label': bridge_node.label,
                'via_bridge_type': bridge_node.node_type,
                'temporal': {
                    'start_date': str(other2.start_date) if other2.start_date else None,
                    'end_date': str(other2.end_date) if other2.end_date else None,
                    'start_date_prose': other2.start_date_prose,
                    'end_date_prose': other2.end_date_prose,
                    'valid_during': other2.valid_during,
                },
            })
            seen_2hop_entity_ids.add(str(other2.id))
            per_bridge += 1
            if per_bridge >= _HOP2_MAX_ENDPOINTS_PER_BRIDGE:
                break

    if seen_2hop_entity_ids:
        logger.info(
            "[entity_cards_v2] %s: added %d 2-hop Entity fact(s) via "
            "State/Event/Goal bridges",
            entity.label, len(seen_2hop_entity_ids),
        )

    said = _said_dates(session, {nid for f in facts for nid in f['source_node_ids']})
    for f in facts:
        dates = [said[nid] for nid in f['source_node_ids'] if nid in said]
        f['said'] = {'first': min(d[0] for d in dates), 'last': max(d[1] for d in dates)} if dates else None
    return facts


def _said_dates(session: Session, node_ids: set) -> Dict[str, tuple]:
    """node_id -> (first, last) date its claim was said, from the source messages' timestamps (kg_node_evidence).
    Not Node.first_observed: for nodes minted by the legacy chat import it is the import date (2026-05-12 for a
    claim said 2025-01-02), so 2,206 of 7,884 nodes read as said more than 30 days later than they were."""
    if not node_ids:
        return {}
    from sqlalchemy import bindparam, text
    rows = session.execute(text(
        "SELECT node_id, MIN(message_timestamp), MAX(message_timestamp) FROM kg_node_evidence "
        "WHERE message_timestamp IS NOT NULL AND node_id IN :ids GROUP BY node_id"
    ).bindparams(bindparam('ids', expanding=True)), {'ids': list(node_ids)}).fetchall()
    return {r[0]: (str(r[1])[:10], str(r[2])[:10]) for r in rows}


def _said_line(said: Optional[Dict[str, str]]) -> str:
    if not said:
        return "said: (no source message date)"
    if said['first'] == said['last']:
        return f"said: {said['last']}"
    return f"said: first {said['first']}, last {said['last']}"


# --- Selection policy helpers ----------------------------------------------

def _contact_tagged_node_ids(session: Session, node_ids: List[str]) -> set:
    """Return the subset of ``node_ids`` carrying an active card-namespace
    `contact` section tag. Used by _collect_candidate_facts to keep
    phone/email/address facts past the importance floor — see
    CARD_FACT_FLOOR comment for why."""
    if not node_ids:
        return set()
    from app.assistant.kg.db.knowledge_graph_db_sqlite import NodeSectionTag
    from app.assistant.kg.section_tagging import (
        NAMESPACE_CARD, CARD_BUILDER_VERSION, node_content_hash,
        tag_is_active_for_build,
    )
    tag_rows = (
        session.query(NodeSectionTag)
        .filter(
            NodeSectionTag.namespace == NAMESPACE_CARD,
            NodeSectionTag.section_name == 'contact',
            NodeSectionTag.node_id.in_(set(node_ids)),
        ).all()
    )
    if not tag_rows:
        return set()
    nodes_by_id = {
        n.id: n for n in
        session.query(Node).filter(Node.id.in_({t.node_id for t in tag_rows})).all()
    }
    result: set = set()
    for tag in tag_rows:
        node = nodes_by_id.get(tag.node_id)
        if node is None:
            continue
        if tag_is_active_for_build(
            tag,
            current_content_hash=node_content_hash(node),
            current_builder_version=CARD_BUILDER_VERSION,
        ):
            result.add(tag.node_id)
    return result


# --- Implementation: try-and-mark drop tracking -----------------------------

def _mark_section_drops(
    session: Session,
    tags_for_section: List[Dict[str, Any]],
    persisted_bullets: List[Dict[str, Any]],
    section_name: str,
    fact_by_id: Dict[str, Dict[str, Any]],
) -> None:
    """Compare tagged input facts vs persisted output bullets. Any tagged
    fact whose primary node never appears in any output bullet was rejected
    by renderer/distiller; persist that rejection on its tag row so the
    next refresh skips it.

    Reconsideration is automatic when the node's content hash changes or
    CARD_BUILDER_VERSION is bumped — see section_tagging.tag_is_active_for_build.
    """
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.assistant.kg.section_tagging import (
        NAMESPACE_CARD, CARD_BUILDER_VERSION,
        mark_facts_dropped, node_content_hash,
    )

    if not tags_for_section:
        return
    # Input node_ids tagged for this section.
    input_node_ids: set[str] = set()
    for t in tags_for_section:
        f = fact_by_id.get(t.get('fact_id'))
        if not f:
            continue
        for nid in (f.get('source_node_ids') or []):
            input_node_ids.add(nid)
    if not input_node_ids:
        return
    # Output node_ids that survived to persisted bullets.
    output_node_ids: set[str] = set()
    for b in (persisted_bullets or []):
        for nid in (b.get('source_node_ids') or []):
            output_node_ids.add(nid)
    dropped = input_node_ids - output_node_ids
    if not dropped:
        return
    # Compute content hashes for the dropped nodes.
    nodes = session.query(Node).filter(Node.id.in_(dropped)).all()
    hashes = {n.id: node_content_hash(n) for n in nodes}
    n = mark_facts_dropped(
        session, NAMESPACE_CARD, section_name, dropped,
        node_content_hashes=hashes,
        builder_version=CARD_BUILDER_VERSION,
    )
    if n > 0:
        logger.info(
            "[entity_cards_v2] section=%s marked %d/%d facts as dropped (try-and-mark)",
            section_name, n, len(dropped),
        )


# --- Implementation: read persisted tags ------------------------------------

def _tags_from_persisted(
    session: Session, facts: List[Dict[str, Any]], bullet_sections: List[str],
) -> List[Dict[str, Any]]:
    """Map facts → tag dicts using persisted kg_node_section_tag (namespace='card').

    Filters out tags whose ``dropped_at`` is set AND whose node content hash
    + builder version both match the drop snapshot — those were previously
    considered and rejected by the renderer/distiller; skipping them avoids
    re-paying LLM cost to re-discover that they don't make the cut. A node
    whose content changed since drop, OR whose drop was recorded under an
    older builder version, gets reconsidered.

    Returns a list of {fact_id, section} entries — same shape as the live
    section_tagger output so downstream code is unchanged. A node tagged
    into multiple sections emits multiple entries.
    Returns an empty list if NO fact's primary node has any ACTIVE persisted
    card tags — the caller treats that as "no data yet, fall back to live".
    """
    from app.assistant.kg.db.knowledge_graph_db_sqlite import NodeSectionTag, Node
    from app.assistant.kg.section_tagging import (
        NAMESPACE_CARD, CARD_BUILDER_VERSION, node_content_hash,
        tag_is_active_for_build,
    )

    if not facts:
        return []
    node_ids = {f['source_node_ids'][0] for f in facts if f.get('source_node_ids')}
    if not node_ids:
        return []
    # Pull full tag rows + the underlying node so we can compute current hashes.
    tag_rows: List[NodeSectionTag] = (
        session.query(NodeSectionTag)
        .filter(
            NodeSectionTag.namespace == NAMESPACE_CARD,
            NodeSectionTag.node_id.in_(node_ids),
        )
        .all()
    )
    if not tag_rows:
        return []
    nodes_by_id = {
        n.id: n for n in
        session.query(Node).filter(Node.id.in_(node_ids)).all()
    }
    # Build {node_id -> [active section names]} after applying the drop filter.
    tags_by_node: Dict[str, List[str]] = {}
    tagged_node_ids = {tag.node_id for tag in tag_rows}
    for tag in tag_rows:
        node = nodes_by_id.get(tag.node_id)
        if node is None:
            continue
        h = node_content_hash(node)
        if not tag_is_active_for_build(
            tag,
            current_content_hash=h,
            current_builder_version=CARD_BUILDER_VERSION,
        ):
            continue
        tags_by_node.setdefault(tag.node_id, []).append(tag.section_name)
    valid = set(bullet_sections)
    # Catch-all default. Facts that survived the importance/NOW filter are
    # card-worthy by definition (someone or something has already vouched
    # for them); the tagger's job is precision placement, not gatekeeping.
    # When a fact has no persisted card tag, OR its tag points at a
    # section not in this entity's template, default to general_facts so
    # nothing card-worthy silently disappears. Matches the existing
    # "reject reroutes to general_facts" pattern but applies to the
    # untagged-by-omission case too. A fact whose card tags were ALL dropped
    # (the renderer or distiller rejected it, unchanged since) is skipped:
    # until 2026-10-05 it fell through to general_facts here and came back on
    # every build ("regular Zoom call every Thursday", dropped 2026-05-22,
    # led a friend's card in 2026-09 though the call moved to Mondays).
    default_section = (
        "general_facts" if "general_facts" in valid
        else (bullet_sections[-1] if bullet_sections else None)
    )
    out: List[Dict[str, Any]] = []
    for f in facts:
        nid = f['source_node_ids'][0] if f.get('source_node_ids') else None
        if not nid:
            continue
        if nid in tagged_node_ids and not tags_by_node.get(nid):
            continue  # every card tag dropped: the build skips it (drop ledger)
        emitted = False
        for sec in tags_by_node.get(nid, []):
            if sec not in valid:
                continue
            out.append({'fact_id': f['fact_id'], 'section': sec})
            emitted = True
        if not emitted and default_section is not None:
            out.append({'fact_id': f['fact_id'], 'section': default_section})
    return out


# --- Implementation: section_tagger LLM call --------------------------------

def _scope():
    """Pipeline scope context for v2 card agents — needed for resource injection.

    Permission is declared in app/assistant/pipelines/entity_cards_v2/scope.yaml.
    """
    from app.assistant.scope.loader import load_scope_for_source
    return load_scope_for_source(
        kind="pipeline", source_id="entity_cards_v2", actor_id="entity_cards_v2",
    )


def _run_section_tagger(
    entity: Node, facts: List[Dict[str, Any]], bullet_sections: List[str],
) -> List[Dict[str, Any]]:
    """Call entity_cards::section_tagger. Returns list of {fact_id, section, ...}.

    `bullet_sections` is the list of LLM-rendered section names — alias/contact/
    summary/level_0 are built deterministically downstream, so they aren't
    valid tagger destinations.
    """
    from app.assistant.ServiceLocator.service_locator import DI

    agent = DI.agent_factory.create_agent("entity_cards::section_tagger")
    if agent is None:
        raise RuntimeError("agent entity_cards::section_tagger not available")

    section_list_str = "\n".join(f"  - {s}" for s in bullet_sections) + "\n  - reject"

    fact_lines = []
    for f in facts:
        arrow = '-->' if f['direction'] == 'outgoing' else '<--'
        line = (
            f"  fact_id={f['fact_id']}\n"
            f"    {entity.label} {arrow} ({f['edge_type']}) {f['object_type']} {f['object_label']!r}\n"
            f"    edge sentence: {(f['edge_sentence'] or '')[:200]!r}\n"
            f"    object sentence: {(f['object_sentence'] or '')[:200]!r}\n"
            f"    confidence_tier: {f['confidence_tier']}\n"
            f"    temporal: start={f['temporal']['start_date']} end={f['temporal']['end_date']}"
            f" valid_during={f['temporal']['valid_during']}"
        )
        fact_lines.append(line)

    agent_input = {
        "entity_descriptor": (
            f"{entity.label} ({entity.node_type}, category={entity.category!r})\n"
            f"original_sentence: {(entity.original_sentence or '')[:200]!r}\n"
            f"description: {(entity.description or '')[:300]!r}"
        ),
        "section_list": section_list_str,
        "candidate_facts": "\n\n".join(fact_lines),
    }

    # Every fact must be tagged: an untagged fact would silently leave the card.
    fact_ids = [f['fact_id'] for f in facts]
    data = _ask_with_one_correction(
        agent, f"section_tagger for {entity.label!r}", agent_input,
        lambda d: _tagger_problems(d, fact_ids),
    )
    return [tf for tf in data['tagged_facts'] if isinstance(tf, dict)]


def _tagger_problems(data: Dict[str, Any], fact_ids: List[str]) -> List[str]:
    tagged = {tf.get('fact_id') for tf in (data.get('tagged_facts') or []) if isinstance(tf, dict)}
    missing = [fid for fid in fact_ids if fid not in tagged]
    if missing:
        return [f"fact_ids {missing} have no tagged_facts entry: tag every fact (or 'reject' it)"]
    return []


# --- Implementation: bullet_renderer LLM call -------------------------------

def _run_bullet_renderer(
    entity: Node, section_name: str, source_facts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Call entity_cards::bullet_renderer with ALL atomic facts for one
    section. Returns list of {bullet_text, source_fact_ids} dicts.

    The renderer fuses near-duplicate atomic facts and emits a sidecar
    mapping each bullet back to its source fact_ids (matches wiki's
    bullet-sidecar pattern for incremental refresh).
    """
    from app.assistant.ServiceLocator.service_locator import DI

    agent = DI.agent_factory.create_agent("entity_cards::bullet_renderer")
    if agent is None:
        raise RuntimeError("agent entity_cards::bullet_renderer not available")

    fact_blocks = []
    for f in source_facts:
        arrow = '-->' if f['direction'] == 'outgoing' else '<--'
        block = (
            f"  fact_id={f['fact_id']}\n"
            f"    {entity.label} {arrow} ({f['edge_type']}) {f['object_label']!r}\n"
            f"    edge: {(f['edge_sentence'] or '')!r}\n"
            f"    target node: {(f['object_sentence'] or '')!r}\n"
            f"    temporal: start={f['temporal']['start_date']} end={f['temporal']['end_date']}"
            f" valid_during={f['temporal']['valid_during']}\n"
            f"    {_said_line(f.get('said'))}"
        )
        fact_blocks.append(block)

    agent_input = {
        "entity_descriptor": (
            f"{entity.label} ({entity.node_type}, category={entity.category!r})"
        ),
        "section_name": section_name,
        "source_facts": "\n\n".join(fact_blocks),
    }

    # Dropping a fact is the renderer's call; a bullet with no text or citing
    # no input fact is an incomplete answer.
    fact_ids = {f['fact_id'] for f in source_facts}
    data = _ask_with_one_correction(
        agent, f"bullet_renderer for {entity.label!r} / {section_name}", agent_input,
        lambda d: _sourced_bullet_problems(d, 'source_fact_ids', fact_ids),
    )
    return [
        {'bullet_text': rb['bullet_text'].strip(), 'source_fact_ids': rb['source_fact_ids']}
        for rb in (data.get('bullets') or [])
    ]


def _sourced_bullet_problems(data: Dict[str, Any], source_key: str, known_ids: set) -> List[str]:
    """Each bullet needs text and a non-empty `source_key` naming only input ids."""
    problems = []
    for i, b in enumerate(data.get('bullets') or [], 1):
        if not isinstance(b, dict) or not (b.get('bullet_text') or '').strip():
            problems.append(f"bullet {i} has no bullet_text")
            continue
        ids = b.get(source_key) or []
        if not ids or any(x not in known_ids for x in ids):
            problems.append(f"bullet {i} {source_key} {ids} must name input ids only, at least one")
    return problems


# --- Implementation: persistence --------------------------------------------

def _persist_card(
    session: Session, entity: Node,
    section_template: List[tuple],
    section_bullets: Dict[str, List[Dict[str, Any]]],
    section_intros: Dict[str, str],
    *,
    activate_new: bool,
) -> Dict[str, Any]:
    """Atomic write: delete existing card content, insert new content.
    A card created here is active only when `activate_new`.

    Runs inside the caller's db_manager transaction — the caller's context
    commits (or rolls back) the whole card write.

    section_template is [(section_name, section_kind), ...] in display order.
    bullets-kind sections get bullets from section_bullets; other kinds
    get intro_text from section_intros.
    """
    card = session.query(EntityCardV2).filter(
        EntityCardV2.entity_node_id == entity.id
    ).first()
    if card is None:
        card = EntityCardV2(
            id=str(uuid.uuid4()),
            entity_node_id=entity.id,
            entity_type=entity.node_type,
            is_active=activate_new,
        )
        session.add(card)
        session.flush()
    else:
        for s in card.sections:
            session.delete(s)
        session.flush()

    # Denormalized lookup fields. Catalog + retrieval read these directly
    # so non-kg cards (with no Node behind them) live alongside kg cards
    # without a JOIN. Rewritten on every build so renames in the KG propagate.
    card.card_title = entity.label
    card.card_aliases = list(entity.aliases) if entity.aliases else None

    n_sections = 0
    n_bullets = 0
    for position, (section_name, kind) in enumerate(section_template):
        bullets = section_bullets.get(section_name, []) if kind == BULLETS else []
        intro_text = section_intros.get(section_name) if kind != BULLETS else None
        if not bullets and not intro_text:
            continue
        section = EntityCardSection(
            id=str(uuid.uuid4()),
            card_id=card.id,
            section_name=section_name,
            position=position,
            intro_text=intro_text,
            intro_source_hash=_compute_hash(intro_text) if intro_text else None,
        )
        session.add(section)
        session.flush()
        n_sections += 1

        for bp, b in enumerate(bullets):
            bullet = EntityCardBullet(
                id=str(uuid.uuid4()),
                section_id=section.id,
                position=bp,
                bullet_text=b['bullet_text'],
                source_node_ids=b['source_node_ids'],
                source_edge_ids=b['source_edge_ids'],
                source_hash=_compute_hash({k: v for k, v in b.items() if k != 'last_said'}),
                confidence_tier=b['confidence_tier'],
            )
            session.add(bullet)
            session.flush()
            for nid in b['source_node_ids']:
                session.add(EntityCardBulletSourceNode(
                    bullet_id=bullet.id,
                    node_id=nid,
                ))
            n_bullets += 1

    card.last_built_at = utc_now()
    card.last_full_rebuild_at = utc_now()

    return {
        'card_id': card.id,
        'entity_label': entity.label,
        'sections': n_sections,
        'bullets': n_bullets,
    }


# --- Section distiller (stage 3 — salience cut) -----------------------------

def _run_section_distiller(
    entity: Node,
    section_name: str,
    renderer_bullets: List[Dict[str, Any]],
    max_bullets: int,
) -> List[Dict[str, Any]]:
    """Call entity_cards::section_distiller. Returns list of
    {bullet_text, source_bullet_ids} dicts, capped at max_bullets.

    Input renderer_bullets must have a 'bullet_id' field (b1, b2, ...) that
    the distiller references in source_bullet_ids.
    """
    from app.assistant.ServiceLocator.service_locator import DI

    agent = DI.agent_factory.create_agent("entity_cards::section_distiller")
    if agent is None:
        raise RuntimeError("agent entity_cards::section_distiller not available")

    bullet_block = "\n".join(
        f"  {rb['bullet_id']}: {rb['bullet_text']} (last said {rb.get('last_said') or 'unknown'})"
        for rb in renderer_bullets
    )
    agent_input = {
        "entity_descriptor": (
            f"{entity.label} ({entity.node_type}, category={entity.category!r})"
        ),
        "section_name": section_name,
        "max_bullets": str(max_bullets),
        "candidate_bullets": bullet_block,
    }
    bullet_ids = {rb['bullet_id'] for rb in renderer_bullets}
    data = _ask_with_one_correction(
        agent, f"section_distiller for {entity.label!r} / {section_name}", agent_input,
        lambda d: _distiller_problems(d, bullet_ids, max_bullets),
    )
    return [
        {'bullet_text': db['bullet_text'].strip(), 'source_bullet_ids': db['source_bullet_ids']}
        for db in data['bullets']
    ]


def _distiller_problems(data: Dict[str, Any], bullet_ids: set, max_bullets: int) -> List[str]:
    """The distiller exists to cut the section to its cap: no bullets, or more
    than the cap, is incomplete."""
    n = len(data.get('bullets') or [])
    if n == 0:
        return [f"no bullets: return the most identity-defining bullets, at most {max_bullets}"]
    if n > max_bullets:
        return [f"{n} bullets returned, more than max_bullets {max_bullets}"]
    return _sourced_bullet_problems(data, 'source_bullet_ids', bullet_ids)


# --- Deterministic + summary section builders -------------------------------

def _build_alias_section(entity: Node) -> Optional[str]:
    """Deterministic alias section: read node.aliases JSON column.

    (Earlier versions of this builder also consulted a `label_alias` table
    from the deleted standardization system; that table is gone now.)
    """
    aliases: List[str] = []
    raw = entity.aliases
    if raw:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        if isinstance(raw, list):
            for a in raw:
                if isinstance(a, str) and a.strip() and a.strip() != entity.label:
                    aliases.append(a.strip())
    if not aliases:
        return None
    return "Also known as: " + ", ".join(aliases) + "."


def _build_summary_section(
    entity: Node,
    section_bullets: Dict[str, List[Dict[str, Any]]],
    alias_text: Optional[str],
) -> str:
    """LLM-condense all rendered bullets (and aliases) into a 2-3 sentence summary.

    Contact info is intentionally excluded — summaries don't quote
    phone numbers / emails.

    A card with no summary is a failed build, so every miss raises: nothing to
    summarize, no agent, a failed call, or an empty summary after one
    correction. Returning None used to persist a summary-less card stamped
    freshly built, its level_0 cut from the wiki-derived description.
    """
    if not section_bullets and not alias_text:
        raise RuntimeError(f"card for {entity.label!r}: no bullets or aliases survived to summarize")

    from app.assistant.ServiceLocator.service_locator import DI

    agent = DI.agent_factory.create_agent("entity_cards::summary_writer")
    if agent is None:
        raise RuntimeError("agent entity_cards::summary_writer not available")

    section_blocks: List[str] = []
    for section_name, bullets in section_bullets.items():
        section_blocks.append(f"### {section_name}")
        for b in bullets:
            section_blocks.append(f"- {b['bullet_text']} (last said {b.get('last_said') or 'unknown'})")

    agent_input = {
        "entity_descriptor": (
            f"{entity.label} ({entity.node_type}, category={entity.category!r})"
        ),
        "alias_line": alias_text or "(none)",
        "card_bullets": "\n".join(section_blocks) if section_blocks else "(no bullets)",
    }

    data = _ask_with_one_correction(
        agent, f"summary_writer for {entity.label!r}", agent_input,
        lambda d: [] if (d.get('summary') or '').strip() else ["the summary is empty: write the 2-3 sentence summary"],
    )
    return data['summary'].strip()


def _build_level_0(summary_text: str) -> str:
    """Level 0 = the absolute minimal tagline: the first sentence of the summary."""
    for sep in ('. ', '! ', '? '):
        if sep in summary_text:
            return summary_text.split(sep, 1)[0].strip() + sep.strip()
    return summary_text.strip()


# --- Helpers ----------------------------------------------------------------

def _latest(saids: List[Optional[Dict[str, str]]]) -> Optional[str]:
    """The latest date any of these facts was said, or None when none has a source message date."""
    return max((s['last'] for s in saids if s), default=None)


def _compute_hash(payload: Any) -> str:
    """Stable content hash of source facts. Used for bullet drift detection."""
    s = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(s.encode('utf-8')).hexdigest()[:16]
