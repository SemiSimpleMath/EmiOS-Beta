"""Section tagging — persistent per-node tags for downstream projections.

A node may carry tags in multiple namespaces (`card`, `wiki`), and may have
multiple tags within a namespace (e.g., a fact that legitimately appears in
both `marriage_and_family` and `education` wiki sections). Tags are written
once (typically at promotion time) and read by card_builder / wiki page
builder via a SELECT — neither rebuilds the tags at projection time.

See also:
  - knowledge_graph_db_sqlite.NodeSectionTag (the ORM model)
  - agents/kg_node_section_tagger (the LLM that produces tags)
  - pipelines/entity_cards_v2/builder.py (consumer)
  - wiki_generator/page_writer.py (consumer)
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, NodeSectionTag
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now

logger = get_logger(__name__)

# Namespace constants — using strings everywhere is fragile.
NAMESPACE_CARD = "card"
NAMESPACE_WIKI = "wiki"
# Sentinel namespace: marks a node as "the tagger ran and produced no card/wiki
# tags" (reject / empty result). Distinct from "never processed". backfill_untagged_nodes
# selects nodes with NO tag row at all, so without a sentinel a rejected node loops
# through the LLM on every routine run forever. Readers filter by NAMESPACE_CARD/WIKI,
# so this row is invisible to card/wiki building.
NAMESPACE_PROCESSED = "_processed"
PROCESSED_SENTINEL = "none"


# ---------------------------------------------------------------------------
# Section vocabularies
# ---------------------------------------------------------------------------
# Single source of truth for which section keys the tagger may emit per
# namespace. Card keys mirror SECTION_TEMPLATES['Person'] BULLETS sections
# (see entity_card_v2.py). Wiki keys are the typical biographical chapters
# the wiki_writer expects.
#
# When new sections are added to either consumer's templates, mirror them
# here so the tagger emits valid keys.

CARD_SECTION_VOCAB = [
    ("contact", "phone numbers, emails, mailing/home addresses, social handles. Anything actionable as contact info."),
    ("connection_to_user", "Relationships that define this entity's place in the user's life: spouse, parent of the user, child of the user, sibling of the user, close friend/mentor — AND relationships within the user's family circle (parent of the user's kids, sibling of the user's spouse, etc.). 'Katy is Peter's mother' goes here when Peter is the user's son, because that relationship is core to Katy's identity in the user's life. Pets of the household also belong here."),
    ("where_they_are", "active residence, current workplace, current city/country"),
    ("what_they_do", "active occupation, ongoing projects, ongoing responsibilities"),
    ("notes", "active preferences, traits, ongoing context that doesn't fit elsewhere"),
    ("current_connections", "other entities the focal subject currently interacts with (book club, gym, hobby group, etc.). Use for non-family connections; family relationships go in connection_to_user."),
]

WIKI_SECTION_VOCAB = [
    ("identity_and_background", "name, place of birth, family origins, ethnicity, early life"),
    ("education", "schools attended, degrees, academic advisors, fields of study"),
    ("career", "jobs, roles, employers, professional milestones, work history"),
    ("marriage_and_family", "spouse, children, siblings, parents, marriage events, family formation"),
    ("residence", "places lived over time, moves"),
    ("health", "medical conditions, treatments, sleep patterns, fitness"),
    ("interests_and_hobbies", "long-term interests, recurring hobbies, leisure preferences"),
    ("contact", "current phone numbers, emails, addresses preserved for the biographical record"),
    ("relationships", "friends, mentors, collaborators outside of direct family"),
]


# Node types eligible for tagging. Leaf-value Entities (phone numbers, etc.)
# are not card/wiki content themselves; they only appear via the State node
# that carries the claim sentence.
_TAGGABLE_NODE_TYPES = {"State", "Event", "Goal", "Property"}

# Default importance gate. Nodes whose source entity (the Entity that owns the
# State via has_state / participant / etc.) sits below this score are skipped
# at tag-write time — they won't surface on cards or wiki anyway, so tagging
# them is wasted LLM cost. Set to None to disable the gate entirely (used
# during transition before importance is rated everywhere).
#
# Contact bypass: if the LLM tagger returns a `contact` tag for a node, we
# persist regardless of source-entity importance. Contact info (phone, email,
# address) is structurally low-importance by PageRank/LLM rating (leaf-like)
# but critical to surface — so the gate cannot suppress it.
DEFAULT_IMPORTANCE_THRESHOLD: Optional[float] = None


def replace_tags(
    session: Session,
    node_id: str,
    namespace: str,
    section_names: Iterable[str],
    *,
    tagger_version: str = "v1",
) -> int:
    """Replace the tag set for one (node, namespace).

    Wipes existing tags for that node+namespace and writes the new set.
    Idempotent — calling twice with the same inputs yields the same state.
    Caller owns the session/transaction.

    Returns the number of rows inserted.
    """
    if not node_id:
        raise ValueError("node_id is required")
    if not namespace:
        raise ValueError("namespace is required")

    # Wipe prior tags for this (node, namespace). New set replaces old set.
    session.query(NodeSectionTag).filter(
        NodeSectionTag.node_id == node_id,
        NodeSectionTag.namespace == namespace,
    ).delete(synchronize_session=False)

    # Dedup + drop empties before insert.
    cleaned: List[str] = []
    seen = set()
    for s in section_names:
        s = str(s or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)

    now = utc_now()
    for name in cleaned:
        session.add(NodeSectionTag(
            node_id=node_id,
            namespace=namespace,
            section_name=name,
            tagged_at=now,
            tagger_version=tagger_version,
        ))
    return len(cleaned)


def get_tags(
    session: Session, node_id: str, namespace: Optional[str] = None,
) -> List[str]:
    """Return tag names for a node. If namespace is given, only that one;
    else returns all namespaces flattened (rarely what you want — usually
    pass a namespace)."""
    q = session.query(NodeSectionTag.section_name).filter(
        NodeSectionTag.node_id == node_id,
    )
    if namespace is not None:
        q = q.filter(NodeSectionTag.namespace == namespace)
    return [row[0] for row in q.all()]


def nodes_with_tag(
    session: Session, namespace: str, section_name: str,
) -> List[str]:
    """Return node_ids tagged with (namespace, section_name)."""
    rows = session.query(NodeSectionTag.node_id).filter(
        NodeSectionTag.namespace == namespace,
        NodeSectionTag.section_name == section_name,
    ).all()
    return [r[0] for r in rows]


def count_tagged_nodes(session: Session, namespace: Optional[str] = None) -> int:
    """Count distinct nodes that have at least one tag (optionally namespace-scoped)."""
    q = session.query(NodeSectionTag.node_id).distinct()
    if namespace is not None:
        q = q.filter(NodeSectionTag.namespace == namespace)
    return q.count()


# ---------------------------------------------------------------------------
# Try-and-mark drop tracking
# ---------------------------------------------------------------------------
# The card builder (and eventually wiki) doesn't include every tagged fact —
# section_distiller curates to a cap. Facts that lose the curation cut are
# tagged-but-not-rendered. We persist that decision so the next refresh
# doesn't re-pay LLM cost to re-discover them.

# Bump this when section_distiller / bullet_renderer prompt changes
# meaningfully so previously-dropped facts get reconsidered.
CARD_BUILDER_VERSION = "v1"


def node_content_hash(node: Node) -> str:
    """Stable hash of a node's claim content for try-and-mark drop tracking.

    Changes when the underlying claim changes — sentence rewrite, description
    refresh from wiki, label rename. NOT affected by importance/PageRank
    bumps, last_observed updates, or attribute-blob churn.
    """
    import hashlib
    payload = "|".join((
        node.label or "",
        node.original_sentence or "",
        node.description or "",
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def mark_facts_dropped(
    session: Session,
    namespace: str,
    section_name: str,
    dropped_node_ids: Iterable[str],
    *,
    node_content_hashes: dict[str, str],
    builder_version: str,
) -> int:
    """Mark these (node, namespace, section_name) tags as dropped by the
    builder. ``node_content_hashes`` is a dict node_id → hash at drop time.

    Returns the number of rows updated.
    """
    ids = [n for n in dropped_node_ids if n]
    if not ids:
        return 0
    now = utc_now()
    n_updated = 0
    for nid in ids:
        h = node_content_hashes.get(nid)
        if h is None:
            continue
        rows = (
            session.query(NodeSectionTag)
            .filter(
                NodeSectionTag.node_id == nid,
                NodeSectionTag.namespace == namespace,
                NodeSectionTag.section_name == section_name,
            )
            .all()
        )
        for row in rows:
            row.dropped_at = now
            row.dropped_at_node_content_hash = h
            row.dropped_by_version = builder_version
            n_updated += 1
    return n_updated


def tag_is_active_for_build(
    tag: NodeSectionTag, *, current_content_hash: str, current_builder_version: str,
) -> bool:
    """Return True if this tag should be passed to the builder.

    A tag is INACTIVE (skip the build) iff all three hold:
      - dropped_at is set (builder previously rejected this fact)
      - AND the node's content hash hasn't changed since the drop
      - AND the builder version hasn't been bumped since the drop

    Any mismatch → reconsider. NULL dropped_at → always active.
    """
    if tag.dropped_at is None:
        return True
    if tag.dropped_at_node_content_hash != current_content_hash:
        return True  # content changed — re-evaluate
    if tag.dropped_by_version != current_builder_version:
        return True  # builder changed — re-evaluate
    return False


# ---------------------------------------------------------------------------
# Tagging orchestration
# ---------------------------------------------------------------------------

_TAGGER_AGENT_NAME = "kg_node_section_tagger"
_TAG_BATCH_SIZE = 20  # Nodes per LLM call. Keeps each call small + fast.


def _format_sections_block(namespace_vocab: list[tuple[str, str]]) -> str:
    return "\n".join(f"  - `{key}` — {desc}" for key, desc in namespace_vocab)


def _format_namespaces_block() -> str:
    return (
        "### card\n" + _format_sections_block(CARD_SECTION_VOCAB) + "\n\n"
        "### wiki\n" + _format_sections_block(WIKI_SECTION_VOCAB)
    )


def _format_nodes_block(nodes: Sequence[Node]) -> str:
    lines: list[str] = []
    for i, n in enumerate(nodes, 1):
        sentence = (n.original_sentence or "").strip()
        if not sentence:
            sentence = n.description or n.label or "(no sentence)"
        lines.append(
            f"[{i}] node_type={n.node_type} category={n.category or '?'}\n"
            f"    label: {n.label}\n"
            f"    sentence: {sentence}"
        )
    return "\n".join(lines)


from app.assistant.importance.queries import max_source_entity_importance


def tag_nodes_by_id(
    node_ids: Sequence[str],
    *,
    scope_context: "ScopeContext",
    tagger_version: str = "v1",
    importance_threshold: Optional[float] = DEFAULT_IMPORTANCE_THRESHOLD,
) -> dict:
    """Run the multi-namespace tagger on these node ids and persist results.

    - Loads each node from DB.
    - Filters out non-taggable types (Entity, Concept, Pod — these are
      identity nodes, not claim nodes).
    - Filters out nodes with no sentence/description content.
    - **Importance gate (optional, per `importance_threshold`).** Computes
      max source-entity importance per node. If below threshold AND the LLM
      tagger doesn't return a `contact` tag → skip persist. If at/above
      threshold OR contact-tagged → persist. NULL importance (rating not
      yet available) is treated as "let through" — transition-friendly.
    - Batches the rest through the LLM tagger.
    - Writes tags via `replace_tags` (idempotent).

    Pass `importance_threshold=None` to disable the gate entirely.

    Returns stats dict: {tagged, skipped_type, skipped_empty, skipped_low_importance,
    contact_bypassed, errors, card_tags_written, wiki_tags_written}.
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message
    from app.models.db_manager import get_db_manager

    stats = {
        "tagged": 0,
        "skipped_type": 0,
        "skipped_empty": 0,
        "skipped_low_importance": 0,
        "contact_bypassed": 0,
        "marked_processed": 0,
        "errors": 0,
        "card_tags_written": 0,
        "wiki_tags_written": 0,
    }
    node_ids = [nid for nid in (node_ids or []) if nid]
    if not node_ids:
        return stats

    # PLAN under a read session, snapshot to plain objects, close — the LLM
    # batches below must not ride a held session (db_manager discipline;
    # 2026-07-08 KG audit G6). Writes happen in one short transaction per
    # batch.
    from types import SimpleNamespace

    with get_db_manager().read_session() as session:
        rows: list[Node] = (
            session.query(Node).filter(Node.id.in_(node_ids)).all()
        )
        # Precompute source-entity importance per node — one query upfront, not
        # one query per node in the batch loop.
        importance_by_node: dict[str, Optional[float]] = {}
        if importance_threshold is not None:
            for n in rows:
                importance_by_node[n.id] = max_source_entity_importance(session, n.id)

        taggable: list[SimpleNamespace] = []
        for n in rows:
            if (n.node_type or "") not in _TAGGABLE_NODE_TYPES:
                stats["skipped_type"] += 1
                continue
            if not (n.original_sentence or n.description or "").strip():
                stats["skipped_empty"] += 1
                continue
            taggable.append(SimpleNamespace(
                id=n.id, label=n.label, node_type=n.node_type,
                category=n.category, original_sentence=n.original_sentence,
                description=n.description,
            ))

    if not taggable:
        return stats

    factory = DI.agent_factory
    agent = factory.create_agent(_TAGGER_AGENT_NAME)
    if agent is None:
        stats["errors"] += 1
        logger.warning("[section_tagging] agent %s not available", _TAGGER_AGENT_NAME)
        return stats

    namespaces_block = _format_namespaces_block()

    total_batches = (len(taggable) + _TAG_BATCH_SIZE - 1) // _TAG_BATCH_SIZE
    for batch_idx, batch_start in enumerate(
        range(0, len(taggable), _TAG_BATCH_SIZE), start=1,
    ):
        batch = taggable[batch_start : batch_start + _TAG_BATCH_SIZE]
        nodes_block = _format_nodes_block(batch)
        try:
            resp = agent.action_handler(Message(
                agent_input={
                    "nodes_block": nodes_block,
                    "namespaces_block": namespaces_block,
                },
                scope_context=scope_context,
            ))
            data = resp.data if resp and hasattr(resp, "data") else {}
        except Exception as exc:
            stats["errors"] += 1
            logger.exception("[section_tagging] tagger raised: %s", exc)
            continue

        # APPLY: one short write transaction per batch — the next batch's
        # LLM call starts with no session held.
        tagged_ids: set[str] = set()        # got >=1 real card/wiki tag this batch
        gate_skipped_ids: set[str] = set()  # intentionally deferred by importance gate
        with get_db_manager().transaction(op="section_tagging.apply") as session:
            for tn in (data.get("results") or []):
                try:
                    idx = int(tn.get("number"))
                    if not (1 <= idx <= len(batch)):
                        continue
                    node = batch[idx - 1]
                    card_tags: list[str] = []
                    wiki_tags: list[str] = []
                    for ns in (tn.get("tags") or []):
                        name = (ns.get("namespace") or "").strip()
                        sections = ns.get("sections") or []
                        if name == NAMESPACE_CARD:
                            card_tags = sections
                        elif name == NAMESPACE_WIKI:
                            wiki_tags = sections
                    # Importance gate (optional). If threshold is set:
                    #   - source-entity importance unrated (None) → let through (transition)
                    #   - source-entity importance below threshold AND no contact tag → skip
                    #   - has 'contact' tag → bypass gate (contact info always persists)
                    if importance_threshold is not None:
                        src_imp = importance_by_node.get(node.id)
                        if src_imp is not None and src_imp < importance_threshold:
                            if "contact" in card_tags:
                                stats["contact_bypassed"] += 1
                            else:
                                stats["skipped_low_importance"] += 1
                                # Defer (don't sentinel) — gate intends to re-tag once
                                # importance is re-derived. Gate is off by default.
                                gate_skipped_ids.add(node.id)
                                continue

                    n_card = replace_tags(
                        session, node.id, NAMESPACE_CARD,
                        card_tags, tagger_version=tagger_version,
                    )
                    n_wiki = replace_tags(
                        session, node.id, NAMESPACE_WIKI,
                        wiki_tags, tagger_version=tagger_version,
                    )
                    stats["card_tags_written"] += n_card
                    stats["wiki_tags_written"] += n_wiki
                    stats["tagged"] += 1
                    if n_card or n_wiki:
                        tagged_ids.add(node.id)
                except Exception as exc:
                    stats["errors"] += 1
                    logger.exception("[section_tagging] per-node persist failed: %s", exc)

            # Treadmill fix: any node we sent this batch that got NO real tag — a
            # reject/empty LLM result, or one the LLM omitted from `results` — and
            # was not gate-deferred, gets a sentinel row. backfill_untagged_nodes
            # selects nodes with no tag row, so without this rejected nodes are
            # re-sent to the LLM on every 30-min routine run forever.
            for node in batch:
                if node.id in tagged_ids or node.id in gate_skipped_ids:
                    continue
                replace_tags(
                    session, node.id, NAMESPACE_PROCESSED,
                    [PROCESSED_SENTINEL], tagger_version=tagger_version,
                )
                stats["marked_processed"] += 1
        logger.info(
            "[section_tagging] batch %d/%d done — tagged so far: %d",
            batch_idx, total_batches, stats["tagged"],
        )

    logger.info(
        "[section_tagging] tagged=%d marked_processed=%d skipped_type=%d skipped_empty=%d "
        "errors=%d card_tags=%d wiki_tags=%d",
        stats["tagged"], stats["marked_processed"], stats["skipped_type"],
        stats["skipped_empty"], stats["errors"], stats["card_tags_written"],
        stats["wiki_tags_written"],
    )
    return stats


def backfill_untagged_nodes(
    *,
    scope_context: "ScopeContext",
    limit: Optional[int] = None,
    only_node_types: Optional[Sequence[str]] = None,
) -> dict:
    """One-time pass: tag every taggable node that has no tags yet.

    Used to seed kg_node_section_tag with tags for nodes that were promoted
    before the tagger was wired into the promoter. After this runs once,
    the card and wiki builders can rely on persisted tags exclusively.

    Args:
      limit: stop after tagging this many nodes (useful for incremental runs).
      only_node_types: subset of _TAGGABLE_NODE_TYPES to restrict to.

    Returns the cumulative stats dict.
    """
    from app.models.base import get_session

    types_filter = list(only_node_types or _TAGGABLE_NODE_TYPES)

    session = get_session()
    try:
        # Find nodes lacking ANY tag (subquery: node_ids that DO have tags,
        # exclude them).
        tagged_ids_subq = session.query(NodeSectionTag.node_id).distinct().subquery()
        q = session.query(Node.id).filter(
            Node.node_type.in_(types_filter),
            Node.original_sentence.isnot(None),
            Node.original_sentence != "",
            ~Node.id.in_(tagged_ids_subq.select()),
        )
        if limit is not None:
            q = q.limit(limit)
        node_ids = [row[0] for row in q.all()]
    finally:
        session.close()

    logger.info("[section_tagging] backfill: %d nodes to tag", len(node_ids))
    if not node_ids:
        return {"to_tag": 0, "tagged": 0}

    # tag_nodes_by_id handles batching + persistence
    result = tag_nodes_by_id(node_ids, scope_context=scope_context)
    result["to_tag"] = len(node_ids)
    return result
