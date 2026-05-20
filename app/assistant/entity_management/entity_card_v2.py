"""Entity Card v2 — section-tagged, bullet-incremental, NOW-snapshot.

See docs/architecture/12_ENTITY_CARDS_V2_DESIGN.md for the design rationale.

Three core tables + one sibling lookup:

  entity_card_v2                       one row per entity that has a card
  entity_card_section                  one row per section per card
  entity_card_bullet                   one row per bullet per section
  entity_card_bullet_source_node       (bullet_id, node_id) lookup for fast
                                       reverse joins when a KG node changes

The naming uses _v2 suffix during the transition. Once v1 is retired,
the table named `entity_card_v2` will be renamed `entity_card` (singular)
and v1's `entity_cards` (plural) will be dropped.
"""
from __future__ import annotations

import uuid
from sqlalchemy import (
    Column, String, Text, Boolean, Integer,
    ForeignKey, Index, UniqueConstraint, JSON, text,
)
from sqlalchemy.orm import relationship

from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now
from app.models.base import Base


class EntityCardV2(Base):
    """One card per entity (Group A pagerank threshold met).

    The card is structurally section/bullet — content lives in child
    `entity_card_section` and `entity_card_bullet` rows.
    """
    __tablename__ = 'entity_card_v2'

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    # NULL = non-kg card (user-authored, not driven by the KG pipeline).
    # SQL UNIQUE allows multiple NULLs, so we can have many non-kg cards;
    # at most one card per KG entity for kg-backed cards.
    entity_node_id = Column(
        String,
        ForeignKey('kg_node_metadata.id', ondelete='CASCADE'),
        nullable=True,
        index=True,
        unique=True,
    )
    entity_type = Column(String, nullable=False)        # 'Entity' / 'Pod' / 'Manual' / etc.
    is_active = Column(Boolean, nullable=False, default=True)

    # Denormalized lookup fields. For kg cards the builder populates these
    # from the KG node (label, aliases JSON). For non-kg cards the user
    # provides them at create time. Retrieval scans these directly so the
    # same path serves both kinds.
    card_title = Column(String, nullable=True, index=True)
    card_aliases = Column(JSON, nullable=True)

    last_built_at = Column(AwareUtcDateTime, nullable=True)
    last_full_rebuild_at = Column(AwareUtcDateTime, nullable=True)
    last_incremental_at = Column(AwareUtcDateTime, nullable=True)

    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    updated_at = Column(AwareUtcDateTime, nullable=False, default=utc_now, onupdate=utc_now)

    sections = relationship(
        "EntityCardSection",
        back_populates="card",
        cascade="all, delete-orphan",
        order_by="EntityCardSection.position",
    )

    __table_args__ = (
        Index('ix_entity_card_v2_active', 'is_active'),
        # At most one ACTIVE card per card_title. The render path looks
        # cards up by card_title (not by entity_node_id) so two active rows
        # with the same title crash chat_gate's one_or_none() entity lookup.
        # SQLite supports partial indexes; this is the structural rule, not
        # an app-level convention.
        Index(
            'uq_entity_card_v2_active_title',
            'card_title',
            unique=True,
            sqlite_where=text('is_active = 1'),
        ),
    )


class EntityCardSection(Base):
    """One section within a card (Identity, Connection, Where, What, Notes, ...).

    Section composition is determined by the section_tagger agent.
    Sections are positioned by hard-coded entity_type templates initially.
    """
    __tablename__ = 'entity_card_section'

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    card_id = Column(
        String,
        ForeignKey('entity_card_v2.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    section_name = Column(String, nullable=False)
    position = Column(Integer, nullable=False)

    # Optional intro paragraph rendered by section_intro agent.
    # `intro_source_hash` is a hash of the section's bullet sequence —
    # when bullets are added/removed/changed, this drifts and the intro
    # needs regeneration.
    intro_text = Column(Text, nullable=True)
    intro_source_hash = Column(String, nullable=True)

    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    updated_at = Column(AwareUtcDateTime, nullable=False, default=utc_now, onupdate=utc_now)

    card = relationship("EntityCardV2", back_populates="sections")
    bullets = relationship(
        "EntityCardBullet",
        back_populates="section",
        cascade="all, delete-orphan",
        order_by="EntityCardBullet.position",
    )

    __table_args__ = (
        UniqueConstraint('card_id', 'section_name', name='uq_card_section'),
        Index('ix_entity_card_section_card', 'card_id'),
    )


class EntityCardBullet(Base):
    """One bullet (~1 sentence fact) within a section.

    Tied back to its source KG nodes/edges via:
      - source_node_ids JSON (canonical list of KG node ids that fed this bullet)
      - source_edge_ids JSON (KG edge ids)
      - source_hash — content hash of the source state; drives diff
      - reverse lookup via EntityCardBulletSourceNode

    Confidence_tier inherits the WEAKEST source tier (axiom > confirmed >
    provisional > inferred). A bullet built on a provisional source is
    itself provisional; the section/card renderer can hedge or surface
    confidence appropriately.
    """
    __tablename__ = 'entity_card_bullet'

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    section_id = Column(
        String,
        ForeignKey('entity_card_section.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )
    position = Column(Integer, nullable=False)

    bullet_text = Column(Text, nullable=False)
    source_node_ids = Column(JSON, nullable=False, default=list)
    source_edge_ids = Column(JSON, nullable=False, default=list)
    source_hash = Column(String, nullable=False)
    confidence_tier = Column(String, nullable=True, index=True)

    generated_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    updated_at = Column(AwareUtcDateTime, nullable=False, default=utc_now, onupdate=utc_now)

    section = relationship("EntityCardSection", back_populates="bullets")

    __table_args__ = (
        Index('ix_entity_card_bullet_section', 'section_id'),
    )


class EntityCardBulletSourceNode(Base):
    """Fast reverse-lookup: given a changed KG node, find affected bullets.

    Denormalized from EntityCardBullet.source_node_ids. Maintained by the
    bullet writer (insert N rows per bullet, one per source node id).
    Without this, finding affected bullets requires JSON-scanning every
    bullet's source_node_ids array.
    """
    __tablename__ = 'entity_card_bullet_source_node'

    bullet_id = Column(
        String,
        ForeignKey('entity_card_bullet.id', ondelete='CASCADE'),
        primary_key=True,
    )
    node_id = Column(String, primary_key=True, index=True)

    __table_args__ = (
        Index('ix_bullet_source_node_node_id', 'node_id'),
    )


# --- Section templates per entity_type --------------------------------------

# Each section has a `kind` that controls how it's built:
#   bullets   — section_tagger picks facts, bullet_renderer fuses into bullets
#   alias     — deterministic: read node.aliases + label_alias rows
#   contact   — deterministic: walk edges to contact-info nodes (phone/email/...)
#   summary   — LLM, built AFTER bullets exist; condenses everything (sans contact)
#   level_0   — deterministic head: 1-line entity tagline; built AFTER summary
#
# Display order = order in the list. `summary` and `level_0` come first
# visually but are written last (they depend on the bullet sections).

BULLETS = 'bullets'
ALIAS = 'alias'
CONTACT = 'contact'
SUMMARY = 'summary'
LEVEL_0 = 'level_0'


# `general_facts` is the catch-all BULLETS section on every template. The
# section_tagger should only reject facts that aren't really about the
# entity; anything that survives the NOW + importance pre-filters has
# already passed the "should it exist" decision and must land somewhere.
# If the tagger picks `reject` anyway, the builder reroutes the fact to
# `general_facts` so meaningful edges never silently disappear.
SECTION_TEMPLATES = {
    'Person': [
        ('level_0', LEVEL_0),
        ('summary', SUMMARY),
        ('alias', ALIAS),
        ('contact', BULLETS),
        ('connection_to_user', BULLETS),
        ('where_they_are', BULLETS),
        ('what_they_do', BULLETS),
        ('notes', BULLETS),
        ('current_connections', BULLETS),
        ('general_facts', BULLETS),
    ],
    'Place': [
        ('level_0', LEVEL_0),
        ('summary', SUMMARY),
        ('alias', ALIAS),
        ('contact', BULLETS),
        ('location', BULLETS),
        ('characteristics', BULLETS),
        ('connection_to_user', BULLETS),
        ('general_facts', BULLETS),
    ],
    'Pod': [
        ('level_0', LEVEL_0),
        ('summary', SUMMARY),
        ('alias', ALIAS),
        ('content_summary', BULLETS),
        ('connections', BULLETS),
        ('general_facts', BULLETS),
    ],
    'Concept': [
        ('level_0', LEVEL_0),
        ('summary', SUMMARY),
        ('alias', ALIAS),
        ('definition', BULLETS),
        ('relevance', BULLETS),
        ('general_facts', BULLETS),
    ],
    '_default': [
        ('level_0', LEVEL_0),
        ('summary', SUMMARY),
        ('alias', ALIAS),
        ('contact', BULLETS),
        ('connection_to_user', BULLETS),
        ('notes', BULLETS),
        ('general_facts', BULLETS),
    ],
}


def section_template_for(entity_type: str, category: str | None = None) -> list[tuple[str, str]]:
    """Return [(section_name, section_kind), ...] in display order."""
    if category in ('person',):
        return SECTION_TEMPLATES['Person']
    if category in ('city', 'country', 'place', 'location', 'address', 'university'):
        return SECTION_TEMPLATES['Place']
    if entity_type == 'Pod':
        return SECTION_TEMPLATES['Pod']
    if entity_type == 'Concept':
        return SECTION_TEMPLATES['Concept']
    return SECTION_TEMPLATES['_default']


def bullet_section_names(template: list[tuple[str, str]]) -> list[str]:
    """Names of sections where section_tagger should classify facts into."""
    return [name for (name, kind) in template if kind == BULLETS]


# Per-section salience caps. Distiller picks ≤ cap from upstream renderer output.
# The card is a curated identity sketch, NOT a KG dump — total bullets across
# all sections for a typical entity should be ~15-30, biggest at ~50 for the
# user's own card.
SECTION_BULLET_CAPS: dict[str, int] = {
    'connection_to_user':   6,
    'where_they_are':       4,
    'what_they_do':         6,
    'notes':                10,
    'current_connections':  8,
    'location':             4,
    'characteristics':      6,
    'content_summary':      6,
    'connections':          6,
    'definition':           4,
    'relevance':            4,
    'general_facts':        10,  # catch-all — generous cap so meaningful edges aren't truncated
}
DEFAULT_BULLET_CAP = 8

# Renderer input chunk size — when a section has more candidate facts than
# this, we chunk the renderer call so it never has to fuse over too much at
# once. Empirically ~40 keeps cognitive load manageable without breaking
# multi-fact fusion within a chunk.
RENDERER_CHUNK_SIZE = 40


def bullet_cap_for_section(section_name: str) -> int:
    return SECTION_BULLET_CAPS.get(section_name, DEFAULT_BULLET_CAP)


# --- Prompt-injection renderer ---------------------------------------------

def render_v2_card_for_prompt_injection_level(
    session, entity_name: str, *, level: int = 1, sections: list[str] | None = None,
) -> str:
    """Look up a v2 card by entity_name and render to text at the given level.

    Level semantics (mirrors the v1 contract so the pipeline-side injector
    doesn't need to know which schema served the card):

      L0 — one-liner (level_0 section's intro_text)
      L1 — name + summary
      L2 — L1 + bullet sections that describe what they are/do
      L3 — L2 + contact section
      L4 — L3 + relationships + aliases

    When `sections` is provided it overrides `level`: render exactly the
    listed sections in their natural order. Use this when an agent needs a
    non-superset slice (e.g. personal_admin wants `level_0` + `contact` but
    not the middle facts/summary that L3 would pull in). Section names match
    SECTION_TEMPLATES keys: 'level_0', 'summary', 'alias', 'contact',
    'connection_to_user', 'where_they_are', 'what_they_do', 'notes',
    'current_connections', 'general_facts', 'location', 'characteristics',
    'content_summary', 'connections', 'definition', 'relevance'.

    Lookup is by card_title directly — no KG node detour — so non-kg
    cards (user-authored, entity_node_id IS NULL) are first-class.

    Returns "" if no v2 card exists for this entity_name.
    """
    # Defense in depth: a partial unique index enforces "at most one active
    # card per title" at the DB layer (see __table_args__). If that
    # invariant ever holds plural matches here, the index has been
    # bypassed (migration window, manual surgery, fresh DB without index)
    # and we want to know about it — log a hard ERROR, then pick the
    # most-recent so chat_gate doesn't crash. Silent fallback would hide
    # the structural violation that should be fixed at the source.
    matches = (
        session.query(EntityCardV2)
        .filter(EntityCardV2.card_title == entity_name, EntityCardV2.is_active == True)  # noqa: E712
        .order_by(EntityCardV2.last_built_at.desc().nullslast())
        .all()
    )
    if not matches:
        return ""
    if len(matches) > 1:
        import logging
        logging.getLogger(__name__).error(
            "INVARIANT VIOLATED: %d active entity_card_v2 rows with card_title=%r "
            "(ids=%s). The uq_entity_card_v2_active_title partial unique index "
            "should make this impossible. Falling back to most-recent by "
            "last_built_at; investigate how the duplicates were created.",
            len(matches), entity_name, [c.id for c in matches],
        )
    card = matches[0]

    all_sections = (
        session.query(EntityCardSection)
        .filter(EntityCardSection.card_id == card.id)
        .order_by(EntityCardSection.position)
        .all()
    )
    by_name = {s.section_name: s for s in all_sections}

    def _bullets(s: EntityCardSection) -> list[str]:
        if s is None:
            return []
        return [
            b.bullet_text for b in session.query(EntityCardBullet)
            .filter(EntityCardBullet.section_id == s.id)
            .order_by(EntityCardBullet.position).all()
        ]

    # Explicit section-list override — render exactly the requested
    # sections and skip the level-based bundles. Section kind comes from
    # SECTION_TEMPLATES (BULLETS vs intro-text). LEVEL_0 is always
    # intro-text and rendered without a label since it is the entity's
    # tagline. SUMMARY / ALIAS / CONTACT are also intro-text-typed in
    # the templates — but in practice contact data lives in bullets
    # (see contact_section_builder), so we render contact as bullets
    # when bullets exist and fall back to intro_text otherwise.
    if sections:
        parts: list[str] = [f"{entity_name} ({card.entity_type})"]
        bullet_set = {'connection_to_user', 'where_they_are', 'what_they_do',
                      'notes', 'current_connections', 'general_facts',
                      'location', 'characteristics', 'content_summary',
                      'connections', 'definition', 'relevance'}
        for sn in sections:
            s = by_name.get(sn)
            if s is None:
                continue
            if sn == LEVEL_0:
                if s.intro_text:
                    parts.append(s.intro_text.strip())
                continue
            if sn == CONTACT:
                # Contact data is bullets in practice. Use them when
                # present; only fall back to intro_text if bullets empty.
                lines = [f"• {b}" for b in _bullets(s)]
                if lines:
                    parts.append("Contact:")
                    parts.extend(lines)
                elif s.intro_text:
                    parts.append("Contact:")
                    for line in s.intro_text.splitlines():
                        if line.strip():
                            parts.append(f"  {line.strip()}")
                continue
            if sn in bullet_set:
                lines = [f"• {b}" for b in _bullets(s)]
                if lines:
                    parts.append(f"{sn}:")
                    parts.extend(lines)
                continue
            # SUMMARY / ALIAS / anything else: intro_text-typed.
            if s.intro_text:
                parts.append(f"{sn}:")
                for line in s.intro_text.splitlines():
                    if line.strip():
                        parts.append(f"  {line.strip()}")
        return "\n".join(parts).strip() if len(parts) > 1 else ""

    # L0 — bare one-liner
    if level == 0:
        s0 = by_name.get(LEVEL_0)
        if s0 and s0.intro_text:
            return f"{entity_name}: {s0.intro_text.strip()}"
        return ""

    # L1+ — header + summary. entity_type lives on the card row directly
    # (no need to walk back to the KG node, which non-kg cards don't have).
    parts: list[str] = [f"{entity_name} ({card.entity_type})"]
    s_sum = by_name.get(SUMMARY)
    if s_sum and s_sum.intro_text:
        parts.append(s_sum.intro_text.strip())

    if level >= 2:
        # "Facts" — what they do, where they are, notes
        fact_sections = ['what_they_do', 'where_they_are', 'notes',
                         'characteristics', 'definition', 'relevance',
                         'content_summary']
        fact_lines: list[str] = []
        for sn in fact_sections:
            for b in _bullets(by_name.get(sn)):
                fact_lines.append(f"• {b}")
        if fact_lines:
            parts.append("Facts:")
            parts.extend(fact_lines)

    if level >= 3:
        # Contact data is stored as bullets (SECTION_TEMPLATES marks the
        # section BULLETS; contact_section_builder writes bullets). The
        # previous implementation read s_contact.intro_text and silently
        # returned nothing because intro_text is always empty for contact.
        s_contact = by_name.get(CONTACT)
        if s_contact:
            contact_bullets = _bullets(s_contact)
            if contact_bullets:
                parts.append("Contact:")
                for b in contact_bullets:
                    parts.append(f"• {b}")
            elif s_contact.intro_text:
                parts.append("Contact:")
                for line in s_contact.intro_text.splitlines():
                    if line.strip():
                        parts.append(f"  {line.strip()}")

    if level >= 4:
        rel_sections = ['connection_to_user', 'current_connections',
                        'connections', 'location']
        rel_lines: list[str] = []
        for sn in rel_sections:
            for b in _bullets(by_name.get(sn)):
                rel_lines.append(f"• {b}")
        if rel_lines:
            parts.append("Relationships:")
            parts.extend(rel_lines)

        s_alias = by_name.get(ALIAS)
        if s_alias and s_alias.intro_text:
            parts.append(s_alias.intro_text.strip())

    return "\n".join(parts).strip()


# --- NOW filter -------------------------------------------------------------

# Events that are definitional / anchor the present even though they
# happened in the past. These survive the NOW filter.
DEFINITIONAL_EVENT_CATEGORIES = frozenset({
    'birth', 'death', 'wedding', 'marriage', 'marriage_start',
    'graduation', 'move', 'hire', 'meeting', 'creation',
    'relationship_beginning', 'family_formation',
})


def is_now_admissible(
    node_type: str,
    end_date,
    category: str | None,
    goal_status: str | None,
    locked_by_user_at,
    valid_currently=None,
) -> bool:
    """Apply the NOW filter to a KG node.

    Returns True iff the node belongs on a card (vs. relegated to wiki).
    Closed states and past one-off events get filtered out; the wiki picks
    them up via a different projection.

    `valid_currently` (added 2026-05-12): authoritative validity flag set
    by meta_data_add at enrichment time. False = explicit closing evidence
    in the source window (closing event, "after moving away" prose, etc.)
    → reject regardless of structured end_date. None = no closing evidence
    (default) → fall through to the existing structural checks.
    """
    if locked_by_user_at is not None:
        return True  # user-locked overrides
    # Strong signal: explicit closing evidence trumps structural inference.
    if valid_currently is False:
        return False
    if node_type == 'State':
        # Active iff no end_date or end_date in the future.
        if end_date is None:
            return True
        return end_date > utc_now()
    if node_type == 'Goal':
        return goal_status in (None, 'active', 'in_progress', 'pending')
    if node_type == 'Event':
        cat = (category or '').strip().lower()
        return cat in DEFINITIONAL_EVENT_CATEGORIES
    if node_type == 'Property':
        return True  # properties are timeless attributes
    # Entity / Concept / Pod are link targets, not bullets themselves
    return False
