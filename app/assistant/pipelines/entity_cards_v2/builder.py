"""Entity Cards v2 builder + incremental refresh.

Three entry points:

    build_card(entity_node_id)
        Full rebuild for one entity. Walks the KG neighborhood, applies
        the NOW filter, runs section_tagger to classify each surviving
        fact, runs bullet_renderer per fact, persists sections + bullets.

    refresh_card_for_changed_node(changed_node_id)
        Incremental refresh: find every bullet whose source_node_ids
        contains changed_node_id, re-render only those bullets.

    rebuild_all_group_a()
        Iterate every Group A entity (PageRank-thresholded), call
        build_card() per entity. Used post-pipeline-batch to refresh
        cards in bulk.

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
from app.models.base import get_session

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

def build_card(entity_node_id: str) -> Dict[str, Any]:
    """Full rebuild for one entity. Idempotent.

    Walks the KG neighborhood, applies the NOW filter, runs section_tagger
    to classify facts, runs bullet_renderer (chunked) per section, runs
    section_distiller when any section exceeds its cap, builds deterministic
    alias + contact sections, runs summary_writer for the top of the card,
    derives level_0 from the summary, and atomically persists. Existing
    sections are wiped and rewritten — incremental refresh has a separate
    entry point.

    Returns: summary dict with status, sections, and bullets counts.
    """
    session = get_session()
    try:
        entity = session.get(Node, entity_node_id)
        if entity is None:
            return {'status': 'error', 'reason': f'node {entity_node_id} not found'}

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

        # 1. Collect candidate facts (NOW-filtered neighborhood)
        facts = _collect_candidate_facts(session, entity)
        if not facts:
            logger.info("[entity_cards_v2] no candidate facts for %s — skipping", entity.label)
            return {'status': 'skipped', 'reason': 'no candidate facts after NOW filter'}

        # 2. Tag each fact into a BULLET section (or 'reject').
        # Prefer persisted tags from kg_node_section_tag (written at promotion
        # time by section_tagging.tag_nodes_by_id). Fall back to live tagger
        # only if NO facts have persisted tags — that's transition-period
        # behavior; once the backfill lands, the live path becomes dead code.
        section_template = section_template_for(entity.node_type, entity.category)
        bullet_sections = bullet_section_names(section_template)
        tagged = _tags_from_persisted(session, facts, bullet_sections)
        if not tagged:
            logger.info(
                "[entity_cards_v2] no persisted tags for %s — running live section_tagger fallback",
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
        for section_name, tags in by_section.items():
            section_facts = [
                fact_by_id[t['fact_id']] for t in tags
                if t['fact_id'] in fact_by_id
            ]
            if not section_facts:
                continue

            # Stage 2: chunked rendering
            rendered_bullets: List[Dict[str, Any]] = []
            for chunk_start in range(0, len(section_facts), RENDERER_CHUNK_SIZE):
                chunk = section_facts[chunk_start:chunk_start + RENDERER_CHUNK_SIZE]
                try:
                    chunk_out = _run_bullet_renderer(entity, section_name, chunk)
                    rendered_bullets.extend(chunk_out)
                except Exception as e:
                    logger.warning(
                        "bullet_renderer failed for %s chunk %d: %s",
                        section_name, chunk_start // RENDERER_CHUNK_SIZE, e,
                    )

            if not rendered_bullets:
                continue

            # Materialize rendered bullets with id + source-id provenance, so
            # distiller can pick by id and we can union the source nodes/edges.
            renderer_bullets: List[Dict[str, Any]] = []
            for i, rb in enumerate(rendered_bullets):
                fact_ids = rb.get('source_fact_ids') or []
                source_facts = [fact_by_id[fid] for fid in fact_ids if fid in fact_by_id]
                if not source_facts:
                    continue
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
                })

            # Stage 3: distillation — only when section exceeds its cap
            cap = bullet_cap_for_section(section_name)
            if len(renderer_bullets) > cap:
                try:
                    distilled = _run_section_distiller(entity, section_name, renderer_bullets, cap)
                except Exception as e:
                    logger.warning(
                        "section_distiller failed for %s: %s (falling back to renderer output truncated)",
                        section_name, e,
                    )
                    distilled = None
            else:
                distilled = None

            if distilled:
                # Distiller picked + possibly fused; map source_bullet_ids → renderer bullets
                rb_by_id = {b['bullet_id']: b for b in renderer_bullets}
                persisted = []
                for db in distilled:
                    src_ids = db.get('source_bullet_ids') or []
                    source_rbs = [rb_by_id[bid] for bid in src_ids if bid in rb_by_id]
                    if not source_rbs:
                        continue
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
                    worst_tier = min(tiers, key=lambda t: tier_rank.get(t, 1)) if tiers else 'provisional'
                    persisted.append({
                        'bullet_text': db['bullet_text'],
                        'source_node_ids': node_ids,
                        'source_edge_ids': edge_ids,
                        'confidence_tier': worst_tier,
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
            # the renderer/distiller. Persist the rejection so the next refresh
            # doesn't re-pay LLM cost to re-discover it. Reconsideration is
            # automatic when (a) node content changes (hash mismatch) or (b)
            # CARD_BUILDER_VERSION is bumped.
            _mark_section_drops(
                session, by_section[section_name], persisted, section_name, fact_by_id,
            )

        # 5. Build deterministic sections (alias only; contact is now
        # LLM-tagged via section_tagger like every other BULLETS section).
        section_intros: Dict[str, str] = {}
        alias_text = _build_alias_section(entity)
        if alias_text:
            section_intros[ALIAS] = alias_text

        # 6. Build summary (LLM) AFTER bullets — uses bullets + alias, NOT contact
        summary_text = _build_summary_section(entity, section_bullets, alias_text)
        if summary_text:
            section_intros[SUMMARY] = summary_text

        # 7. Build level_0 (deterministic head, depends on summary)
        level_0_text = _build_level_0(entity, summary_text)
        if level_0_text:
            section_intros[LEVEL_0] = level_0_text

        # 8. Persist
        result = _persist_card(
            session, entity, section_template, section_bullets, section_intros,
        )
        return {'status': 'ok', **result}
    finally:
        session.close()


def refresh_card_for_changed_node(changed_node_id: str) -> Dict[str, Any]:
    """Incremental refresh: find bullets sourced from this node, re-render them.

    Returns: summary dict with bullet counts.
    """
    session = get_session()
    try:
        rows = session.query(EntityCardBulletSourceNode).filter(
            EntityCardBulletSourceNode.node_id == changed_node_id
        ).all()
        if not rows:
            return {'status': 'noop', 'reason': 'no bullets reference this node'}

        bullet_ids = [r.bullet_id for r in rows]
        affected = session.query(EntityCardBullet).filter(
            EntityCardBullet.id.in_(bullet_ids)
        ).all()

        n_updated = 0
        n_deleted = 0
        for bullet in affected:
            sources = _hydrate_sources(session, bullet)
            # Re-evaluate the NOW filter on the source nodes
            if not _any_source_admits(sources):
                # Source state has moved out of NOW (state closed, etc.)
                session.delete(bullet)
                n_deleted += 1
                continue
            # Re-render the bullet from current source state. The renderer
            # is section-batched and returns a list of {bullet_text, source_fact_ids}
            # dicts; for a single-fact refresh we expect at most one entry.
            entity = session.get(Node, _get_entity_id_for_section(session, bullet.section_id))
            if entity is None:
                continue
            section = session.get(EntityCardSection, bullet.section_id)
            facts = [_fact_from_sources(sources)]
            try:
                rendered = _run_bullet_renderer(entity, section.section_name, facts)
            except Exception as e:
                logger.warning("re-render failed for bullet %s: %s", bullet.id, e)
                continue
            new_text = (rendered[0].get('bullet_text') or '').strip() if rendered else ''
            if new_text and new_text != bullet.bullet_text:
                bullet.bullet_text = new_text
                bullet.source_hash = _compute_hash(facts[0])
                bullet.generated_at = utc_now()
                n_updated += 1

        session.commit()
        return {'status': 'ok', 'updated': n_updated, 'deleted': n_deleted}
    finally:
        session.close()


def rebuild_all_group_a() -> Dict[str, Any]:
    """Iterate Group A entities (PageRank-thresholded), build card for each.

    Used after a pipeline batch completes — the card subscriber drains
    pending refreshes in bulk. Per-entity errors don't abort the run.
    """
    # Reuse the existing Group A logic from the v1 pipeline
    from app.assistant.pipelines.entity_cards.kg_entity_card_pipeline.kg_entity_card_pipeline import (
        _get_group_a_ids,
    )
    group_a = _get_group_a_ids()
    logger.info("[entity_cards_v2] rebuild_all_group_a: %d entities", len(group_a))

    n_built = n_skipped = n_errored = 0
    for nid in group_a:
        try:
            result = build_card(nid)
            if result.get('status') == 'ok':
                n_built += 1
            else:
                n_skipped += 1
        except Exception as e:
            logger.exception("build_card failed for %s: %s", nid, e)
            n_errored += 1

    return {'built': n_built, 'skipped': n_skipped, 'errored': n_errored}


# --- Implementation: candidate fact collection ------------------------------

def _collect_candidate_facts(session: Session, entity: Node) -> List[Dict[str, Any]]:
    """Walk the entity's edges, build a list of NOW-admissible facts.

    A "fact" = one connected node (or its connecting edge) that survives
    the NOW filter. Returns dicts ready to send to section_tagger.

    After collection, the list is sorted by importance descending and
    truncated to CARD_FACT_TOP_K. On sparse entities the truncation is a
    no-op (count < K); on hubs it keeps the most-important slice.
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
            'temporal': {
                'start_date': str(other.start_date) if other.start_date else None,
                'end_date': str(other.end_date) if other.end_date else None,
                'start_date_prose': other.start_date_prose,
                'end_date_prose': other.end_date_prose,
                'valid_during': other.valid_during,
            },
        })

    # Sort by importance descending so downstream rendering chunks see the
    # high-importance facts FIRST. If the section has 30 input facts and the
    # renderer chunks them in groups of 10, the top-importance bullets are
    # guaranteed in the first chunk. NULL importance sorts at the end.
    def _imp(f: Dict[str, Any]) -> float:
        # Scale-aware fallback. When node_importance is NULL (rater hasn't
        # run yet), fall back to edge_importance / 10 so a missing-rating
        # fact lands on the same 0-10 scale as State.importance (which
        # is derived as `max(src_imp × edge_imp / 10)`). Without the /10
        # rescale, fallbacks rated 5 would dominate properly-rated States
        # at 0.5–3, distorting card fact ordering.
        ni = f.get('node_importance')
        if ni is not None:
            return float(ni)
        ei = f.get('edge_importance')
        if ei is not None:
            return float(ei) / 10.0
        return 0
    facts.sort(key=lambda f: -_imp(f))

    # Apply the selection policy (see CARD_FACT_FLOOR comment).
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
    # untagged-by-omission case too.
    default_section = (
        "general_facts" if "general_facts" in valid
        else (bullet_sections[-1] if bullet_sections else None)
    )
    out: List[Dict[str, Any]] = []
    for f in facts:
        nid = f['source_node_ids'][0] if f.get('source_node_ids') else None
        if not nid:
            continue
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
    """Pipeline scope context for v2 card agents — needed for resource injection."""
    from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
    return build_pipeline_scope_context(
        pipeline_id="entity_cards_v2", actor_id="entity_cards_v2",
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

    result = agent.action_handler(Message(agent_input=agent_input, scope_context=_scope()))
    data = result.data if result and hasattr(result, 'data') else {}
    if hasattr(data, 'model_dump'):
        data = data.model_dump()
    if not isinstance(data, dict):
        return []
    out = []
    for tf in (data.get('tagged_facts') or []):
        if isinstance(tf, dict):
            out.append(tf)
        elif hasattr(tf, 'model_dump'):
            out.append(tf.model_dump())
    return out


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
            f" valid_during={f['temporal']['valid_during']}"
        )
        fact_blocks.append(block)

    agent_input = {
        "entity_descriptor": (
            f"{entity.label} ({entity.node_type}, category={entity.category!r})"
        ),
        "section_name": section_name,
        "source_facts": "\n\n".join(fact_blocks),
    }

    result = agent.action_handler(Message(agent_input=agent_input, scope_context=_scope()))
    data = result.data if result and hasattr(result, 'data') else {}
    if hasattr(data, 'model_dump'):
        data = data.model_dump()
    if not isinstance(data, dict):
        return []
    out = []
    for rb in (data.get('bullets') or []):
        if hasattr(rb, 'model_dump'):
            rb = rb.model_dump()
        if not isinstance(rb, dict):
            continue
        text = (rb.get('bullet_text') or '').strip()
        fids = rb.get('source_fact_ids') or []
        if not text or not fids:
            continue
        out.append({'bullet_text': text, 'source_fact_ids': fids})
    return out


# --- Implementation: persistence --------------------------------------------

def _persist_card(
    session: Session, entity: Node,
    section_template: List[tuple],
    section_bullets: Dict[str, List[Dict[str, Any]]],
    section_intros: Dict[str, str],
) -> Dict[str, Any]:
    """Atomic write: delete existing card content, insert new content.

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
            is_active=True,
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
                source_hash=_compute_hash(b),
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
    session.commit()

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
        f"  {rb['bullet_id']}: {rb['bullet_text']}"
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
    result = agent.action_handler(Message(agent_input=agent_input, scope_context=_scope()))
    data = result.data if result and hasattr(result, 'data') else {}
    if hasattr(data, 'model_dump'):
        data = data.model_dump()
    if not isinstance(data, dict):
        return []
    out = []
    for db in (data.get('bullets') or []):
        if hasattr(db, 'model_dump'):
            db = db.model_dump()
        if not isinstance(db, dict):
            continue
        text = (db.get('bullet_text') or '').strip()
        src_ids = db.get('source_bullet_ids') or []
        if not text or not src_ids:
            continue
        out.append({'bullet_text': text, 'source_bullet_ids': src_ids})
    return out


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
) -> Optional[str]:
    """LLM-condense all rendered bullets (and aliases) into a 2-3 sentence summary.

    Contact info is intentionally excluded — summaries don't quote
    phone numbers / emails.
    """
    if not section_bullets and not alias_text:
        return None

    from app.assistant.ServiceLocator.service_locator import DI

    agent = DI.agent_factory.create_agent("entity_cards::summary_writer")
    if agent is None:
        logger.warning("entity_cards::summary_writer not available — skipping summary")
        return None

    section_blocks: List[str] = []
    for section_name, bullets in section_bullets.items():
        section_blocks.append(f"### {section_name}")
        for b in bullets:
            section_blocks.append(f"- {b['bullet_text']}")

    agent_input = {
        "entity_descriptor": (
            f"{entity.label} ({entity.node_type}, category={entity.category!r})"
        ),
        "alias_line": alias_text or "(none)",
        "card_bullets": "\n".join(section_blocks) if section_blocks else "(no bullets)",
    }

    try:
        result = agent.action_handler(Message(agent_input=agent_input, scope_context=_scope()))
    except Exception as e:
        logger.warning("summary_writer failed for %s: %s", entity.label, e)
        return None

    data = result.data if result and hasattr(result, 'data') else {}
    if hasattr(data, 'model_dump'):
        data = data.model_dump()
    if not isinstance(data, dict):
        return None
    return (data.get('summary') or '').strip() or None


def _build_level_0(entity: Node, summary_text: Optional[str]) -> Optional[str]:
    """Level 0 = the absolute minimal tagline. Deterministic head from summary.

    Takes the first sentence of the summary. If no summary, falls back
    to the entity descriptor.
    """
    if summary_text:
        # First sentence (terminated by . ! or ?). Keep it short.
        for sep in ('. ', '! ', '? '):
            if sep in summary_text:
                return summary_text.split(sep, 1)[0].strip() + sep.strip()
        return summary_text.strip()
    # Fallback
    if entity.description:
        return entity.description[:200].strip()
    return None


# --- Helpers ----------------------------------------------------------------

def _compute_hash(payload: Any) -> str:
    """Stable content hash of source facts. Used for bullet drift detection."""
    s = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(s.encode('utf-8')).hexdigest()[:16]


def _hydrate_sources(session: Session, bullet: EntityCardBullet) -> List[Node]:
    return [n for n in (session.get(Node, nid) for nid in bullet.source_node_ids) if n is not None]


def _any_source_admits(nodes: List[Node]) -> bool:
    for n in nodes:
        if is_now_admissible(n.node_type, n.end_date, n.category,
                             n.goal_status, n.locked_by_user_at,
                             valid_currently=getattr(n, "valid_currently", None)):
            return True
    return False


def _get_entity_id_for_section(session: Session, section_id: str) -> Optional[str]:
    s = session.get(EntityCardSection, section_id)
    if s is None:
        return None
    card = session.get(EntityCardV2, s.card_id)
    return card.entity_node_id if card else None


def _fact_from_sources(nodes: List[Node]) -> Dict[str, Any]:
    """Reconstruct a minimal fact dict from current node state for re-render.

    Synthesizes a fact_id ('f1') because the bullet_renderer prompt
    references it ('fact_id={f['fact_id']}'). For incremental refresh we
    only feed one fact, so a single sentinel id suffices.
    """
    n = nodes[0]
    return {
        'fact_id': 'f1',
        'source_node_ids': [x.id for x in nodes],
        'source_edge_ids': [],
        'object_label': n.label,
        'object_type': n.node_type,
        'object_category': n.category,
        'edge_type': '(refresh)',
        'edge_sentence': None,
        'object_sentence': n.original_sentence,
        'direction': 'outgoing',
        'confidence_tier': n.confidence_tier or 'provisional',
        'temporal': {
            'start_date': str(n.start_date) if n.start_date else None,
            'end_date': str(n.end_date) if n.end_date else None,
            'start_date_prose': n.start_date_prose,
            'end_date_prose': n.end_date_prose,
            'valid_during': n.valid_during,
        },
    }
