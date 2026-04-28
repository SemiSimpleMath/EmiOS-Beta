"""Deterministic card-inputs assembly.

Given an entity label, produce the full structured input package the card
writer LLM will consume:

- entity metadata (name, type, node id, description)
- aliases (direct from Node.aliases)
- deterministic contact_info (dedicated contact edges)
- bullet list (from render_bullets)
- per-bullet tags (tagger, with content-hash cache reused across runs)
- grouped-by-section bullet slices (Dict[section_key, List[Bullet]])

Everything up to tagging is pure code. The tagger itself is an LLM but each
batch is independent and cached, so repeat runs on stable KG data are
near-free.

This module does NOT call the card-writer LLM. That's card_writer.py's job.
Separating them makes the intermediate state inspectable — if a card comes
out wrong, we can check the CardInputs first to see whether the upstream
was the problem or the writer was.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.kg_projection import (
    Bullet,
    EntityNeighborhood,
    SectionSpec,
    get_entity_neighborhood,
    load_tags,
    load_taxonomy,
    render_bullets,
    save_tags,
    sections_as_prompt_list,
    tag_bullets,
)
from app.assistant.pipelines.entity_cards_v2.contact_extractor import (
    ContactEntry,
    extract_contact_info,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
)

logger = get_logger(__name__)

# The tagger agent used for section classification. Shared with the wiki.
TAGGER_AGENT = "wiki_section_tagger"

# Neutral tag-cache location for cards. The wiki caches under its vault; we
# put card caches in a repo-neutral spot. Long-term both should share one
# cache; leaving that unification for later.
DEFAULT_TAG_CACHE_ROOT_RELATIVE = "data/kg_projection"

# Scope for card-side tagger calls — separate from wiki so authorization
# decisions can diverge if needed.
_SCOPE_CARDS = ScopeContext(
    scope_id="scope::entity_cards_v2::card_inputs",
    owner_id="jukka",
    actor_id="entity_cards_v2_card_inputs",
    surface="ui",
    room_id="master_room",
    approval=ScopeApprovalPolicy(authority_level=100),
    resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
)


@dataclass
class CardInputs:
    """Fully-assembled deterministic input for the card-writer LLM."""
    entity_name: str
    entity_type: str
    entity_node_id: str
    description: str
    aliases: List[str] = field(default_factory=list)
    contact_info: List[ContactEntry] = field(default_factory=list)

    # All rendered bullets in the neighborhood.
    bullets: List[Bullet] = field(default_factory=list)

    # Per-section bullet slices — empty sections omitted.
    grouped_bullets: Dict[str, List[Bullet]] = field(default_factory=dict)

    # The section taxonomy that was in effect for this run.
    taxonomy: List[SectionSpec] = field(default_factory=list)

    # Diagnostics: total bullets + tagged counts.
    bullet_count: int = 0
    tagged_section_count: int = 0

    # Full neighborhood handle for advanced consumers (card writer LLM reads
    # the entity_card_v1, if present, as a prior — future step).
    neighborhood: Optional[EntityNeighborhood] = None


def _resolve_tag_cache_root(override: Optional[Path]) -> Path:
    if override is not None:
        return Path(override)
    try:
        from app.assistant.utils.paths import get_repo_root
        root = get_repo_root()
    except Exception:
        root = Path(__file__).resolve().parents[4]
    return Path(root) / DEFAULT_TAG_CACHE_ROOT_RELATIVE


def _make_window_loader():
    """Return a function that resolves window_id → resolved_text.

    A window_id may live in either ``kg_chat_conversation_window_item``
    (legacy time-based windows) or ``kg_window_message`` (current
    LLM-segmented windows). The two UUID spaces are disjoint, so try the
    legacy lookup first and fall through to the current pipeline tables.
    The text is reconstructed by joining message rows in window order.

    Per-call short DB session — never holds a session across LLM calls. Use a
    small per-call cache so repeated lookups in the same tagging run don't
    re-query.
    """
    from sqlalchemy import text as sql_text
    from app.models.base import get_session

    cache: Dict[str, str] = {}

    def _format(rows) -> str:
        lines: List[str] = []
        for role, speaker, body in rows:
            body = (body or "").strip()
            if not body:
                continue
            role = (role or "").strip().lower()
            speaker = (speaker or "").strip() or role or "user"
            if role == "user":
                lines.append(f"User ({speaker}): {body}")
            elif role == "assistant":
                lines.append(f"Assistant: {body}")
            else:
                lines.append(body)
        return "\n".join(lines)

    def loader(window_id: str) -> str:
        if window_id in cache:
            return cache[window_id]
        session = get_session()
        try:
            # Legacy storage — message body lives directly on the item row.
            rows = session.execute(
                sql_text(
                    "SELECT role, speaker_name, text AS body "
                    "FROM kg_chat_conversation_window_item "
                    "WHERE window_id = :w ORDER BY item_order"
                ),
                {"w": window_id},
            ).fetchall()
            text = _format(rows)
            if not text:
                # Current-pipeline storage — message body comes from
                # unified_log_2026, with kg_resolved_message preferred when
                # present (entity-substituted version).
                rows = session.execute(
                    sql_text(
                        "SELECT ul.role, ul.speaker_name, "
                        "       COALESCE(rm.resolved_text, ul.message) AS body "
                        "FROM kg_window_message wm "
                        "JOIN unified_log_2026 ul        ON ul.id = wm.unified_log_id "
                        "LEFT JOIN kg_resolved_message rm ON rm.unified_log_id = wm.unified_log_id "
                        "WHERE wm.window_id = :w "
                        "ORDER BY wm.item_order"
                    ),
                    {"w": window_id},
                ).fetchall()
                text = _format(rows)
        finally:
            session.close()
        cache[window_id] = text
        return text

    return loader


def build_card_inputs(
    entity_label: str,
    *,
    tag_cache_root: Optional[Path] = None,
    scope_context: Optional[ScopeContext] = None,
) -> CardInputs:
    """Assemble a ``CardInputs`` for ``entity_label``.

    Steps (all deterministic except the tagger, which is independent-batch
    LLM with a content-hash cache):

      1. Load the neighborhood.
      2. Pull aliases + description directly from the Node.
      3. Run the deterministic contact extractor on contact-typed edges.
      4. Render bullets with provenance.
      5. Tag bullets (reuses cached tags; only uncached bullets hit the LLM).
      6. Group by section.

    Returns a ``CardInputs`` dataclass with everything the card-writer LLM
    needs as its next input.
    """
    tag_cache_root = _resolve_tag_cache_root(tag_cache_root)
    scope = scope_context or _SCOPE_CARDS

    neighborhood = get_entity_neighborhood(entity_label)
    entity = neighborhood.entity

    aliases = list(entity.aliases or [])
    description = (entity.description or "").strip()
    contact_info = extract_contact_info(neighborhood)

    bullets = render_bullets(neighborhood)
    if not bullets:
        logger.info("No bullets for %s — card inputs will be minimal.", entity_label)
        return CardInputs(
            entity_name=entity.label,
            entity_type=entity.node_type or "Entity",
            entity_node_id=str(entity.id),
            description=description,
            aliases=aliases,
            contact_info=contact_info,
            neighborhood=neighborhood,
        )

    taxonomy = load_taxonomy(scope_context=scope)
    if not taxonomy:
        logger.error(
            "No section taxonomy — card inputs for %s will have all bullets in a single fallback bucket.",
            entity_label,
        )
        return CardInputs(
            entity_name=entity.label,
            entity_type=entity.node_type or "Entity",
            entity_node_id=str(entity.id),
            description=description,
            aliases=aliases,
            contact_info=contact_info,
            bullets=bullets,
            grouped_bullets={"_untagged": list(bullets)},
            taxonomy=[],
            bullet_count=len(bullets),
            tagged_section_count=1,
            neighborhood=neighborhood,
        )

    bullet_texts = [b.text for b in bullets]
    allowed_keys = {s.key for s in taxonomy}
    allowed_block = sections_as_prompt_list(taxonomy)

    # Build the bullet → window-ids map and a lazy window-text loader. The
    # tagger uses these to ground each bullet in its source conversation
    # window — see feedback_full_resolved_window_beats_fragments. Without
    # this, a bullet like "**is parent in** [[Jukka]]" is opaque; with the
    # window text, the LLM sees the surrounding context that produced it.
    bullet_windows = {b.key: b.source_window_ids for b in bullets if b.source_window_ids}
    window_loader = _make_window_loader()

    tagger = DI.agent_factory.create_agent(TAGGER_AGENT)
    if tagger is None:
        raise RuntimeError(f"agent_factory returned None for {TAGGER_AGENT!r}")

    cached_tags = load_tags(tag_cache_root, entity_label)
    tags = tag_bullets(
        tagger,
        entity_label=entity_label,
        entity_type=entity.node_type or "Entity",
        bullets=bullet_texts,
        allowed_sections_block=allowed_block,
        allowed_keys=allowed_keys,
        cached_tags=cached_tags,
        scope_context=scope,
        bullet_windows=bullet_windows,
        window_loader=window_loader,
    )
    save_tags(tag_cache_root, entity_label, tags)

    # Group by section — preserve Bullet objects (not just text).
    grouped: Dict[str, List[Bullet]] = {}
    for b in bullets:
        for sec in tags.get(b.key, []):
            grouped.setdefault(sec, []).append(b)

    return CardInputs(
        entity_name=entity.label,
        entity_type=entity.node_type or "Entity",
        entity_node_id=str(entity.id),
        description=description,
        aliases=aliases,
        contact_info=contact_info,
        bullets=bullets,
        grouped_bullets=grouped,
        taxonomy=taxonomy,
        bullet_count=len(bullets),
        tagged_section_count=len(grouped),
        neighborhood=neighborhood,
    )
