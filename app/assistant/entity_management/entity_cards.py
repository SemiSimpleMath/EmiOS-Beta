"""
Entity Cards Database Model
Stores generated entity cards for prompt injection
"""

import uuid
import json
from datetime import datetime, timezone, timedelta
from sqlalchemy import (
    Column, String, DateTime, Text, Float, Boolean,
    UniqueConstraint, Index, func, Integer, ForeignKey, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

from app.models.base import Base, get_session

# SQLite compatibility: Custom type for JSON arrays
class JSONEncodedList(TypeDecorator):
    """Represents a list as JSON-encoded string for SQLite."""
    impl = Text
    cache_ok = True
    
    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        # Enforce canonical shape: lists only (or a JSON string representing a list).
        if isinstance(value, list):
            return json.dumps(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception as e:
                raise TypeError("Expected list or JSON list string for JSONEncodedList") from e
            if isinstance(parsed, list):
                return json.dumps(parsed)
            raise TypeError("Expected list or JSON list string for JSONEncodedList")
        raise TypeError("Expected list or JSON list string for JSONEncodedList")
    
    def process_result_value(self, value, dialect):
        if value is not None:
            return json.loads(value)
        return None


class EntityCard(Base):
    """
    Entity Card table for storing generated entity cards
    """
    __tablename__ = 'entity_cards'
    
    # Primary key (SQLite: UUID as TEXT)
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    
    # Core entity information
    entity_name = Column(String(255), nullable=False, index=True)
    entity_type = Column(String(100), nullable=False)  # Removed index=True since we have explicit Index
    source_node_id = Column(String, nullable=True)  # Reference to KG node if applicable (UUID as TEXT)
    
    # Original KG data
    original_description = Column(Text, nullable=True)  # Original description from KG node
    original_aliases = Column(JSONEncodedList, nullable=True)  # Original aliases from KG node
    
    # Generated content
    summary = Column(Text, nullable=False)
    key_facts = Column(JSONEncodedList, nullable=True)  # Array of key facts
    relationships = Column(JSONEncodedList, nullable=True)  # Array of relationship descriptions
    user_relevance_reason = Column(Text, nullable=True)  # One-sentence "why this matters to the user"
    
    # Metadata
    aliases = Column(JSONEncodedList, nullable=True)  # Alternative names for the entity (processed)
    confidence = Column(Float, nullable=True)  # Confidence score of the generation
    
    # Processing metadata
    batch_number = Column(Integer, nullable=True)  # Which batch this was processed in
    total_batches = Column(Integer, nullable=True)  # Total batches for the entity
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    
    # Additional metadata stored as JSON
    # Canonical shape: dict. (Legacy shapes like list-of-pairs or JSON-string are coerced at read time.)
    card_metadata = Column(JSON, nullable=False, default=dict)
    
    # Status tracking
    is_active = Column(Boolean, default=True, nullable=False)
    last_used = Column(DateTime(timezone=True), nullable=True)  # When last used for prompt injection
    usage_count = Column(Integer, default=0, nullable=False)  # How many times used
    
    __table_args__ = (
        # Unique constraint on entity name to prevent duplicates
        UniqueConstraint('entity_name', name='uq_entity_cards_entity_name'),
        
        # Indexes for efficient querying (SQLite compatible - no GIN indexes)
        Index('ix_entity_cards_entity_type', 'entity_type'),
        Index('ix_entity_cards_created_at', 'created_at'),
        Index('ix_entity_cards_last_used', 'last_used'),
        Index('ix_entity_cards_usage_count', 'usage_count'),
        Index('ix_entity_cards_is_active', 'is_active'),
        {'extend_existing': True}
    )

    # ORM relationships (keep behavior consistent even if SQLite FK cascades are misconfigured)
    usages = relationship(
        "EntityCardUsage",
        back_populates="entity_card",
        # DB-managed cascade (ondelete='CASCADE' + PRAGMA foreign_keys=ON).
        cascade="save-update, merge",
        passive_deletes=True,
    )
    index_terms = relationship(
        "EntityCardIndex",
        back_populates="entity_card",
        # DB-managed cascade (ondelete='CASCADE' + PRAGMA foreign_keys=ON).
        cascade="save-update, merge",
        passive_deletes=True,
    )


class EntityCardUsage(Base):
    """
    Track usage of entity cards for prompt injection
    """
    __tablename__ = 'entity_card_usage'
    
    # Primary key (SQLite: UUID as TEXT)
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    
    # Foreign key to entity card
    entity_card_id = Column(String, ForeignKey('entity_cards.id', ondelete='CASCADE'), nullable=False)
    
    # Usage context
    agent_name = Column(String(100), nullable=True)  # Which agent used the card
    session_id = Column(String(255), nullable=True)  # Session identifier
    query_text = Column(Text, nullable=True)  # The query that triggered the usage
    
    # Usage metadata
    usage_type = Column(String(50), nullable=False, default='prompt_injection')  # Type of usage
    confidence_threshold = Column(Float, nullable=True)  # Confidence threshold used
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    
    # Relationship
    entity_card = relationship("EntityCard", back_populates="usages")
    
    __table_args__ = (
        Index('ix_entity_card_usage_entity_card_id', 'entity_card_id'),
        Index('ix_entity_card_usage_agent_name', 'agent_name'),
        Index('ix_entity_card_usage_session_id', 'session_id'),
        Index('ix_entity_card_usage_created_at', 'created_at'),
    )


class EntityCardIndex(Base):
    """
    Search index for entity cards to enable fast lookup by aliases and variations
    """
    __tablename__ = 'entity_card_index'
    
    # Primary key (SQLite: UUID as TEXT)
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    
    # Foreign key to entity card
    entity_card_id = Column(String, ForeignKey('entity_cards.id', ondelete='CASCADE'), nullable=False)
    
    # Index terms
    index_term = Column(String(255), nullable=False)  # The term to index (alias, variation, etc.)
    term_type = Column(String(50), nullable=False)  # 'alias', 'variation', 'normalized', etc.
    term_priority = Column(Float, default=1.0, nullable=False)  # Priority for matching
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    
    # Relationship
    entity_card = relationship("EntityCard", back_populates="index_terms")
    
    __table_args__ = (
        # Unique constraint to prevent duplicate index terms for same entity
        UniqueConstraint('entity_card_id', 'index_term', name='uq_entity_card_index_term'),
        
        # Indexes for efficient lookup
        Index('ix_entity_card_index_term', 'index_term'),
        Index('ix_entity_card_index_term_type', 'term_type'),
        Index('ix_entity_card_index_priority', 'term_priority'),
    )


# Database management functions
def check_entity_cards_db_exists():
    """Check if entity cards tables exist"""
    from sqlalchemy import inspect
    session = get_session()
    engine = session.bind
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()
    session.close()
    
    required_tables = ['entity_cards', 'entity_card_usage', 'entity_card_index']
    existing_required_tables = [table for table in required_tables if table in existing_tables]
    
    return {
        'exists': len(existing_required_tables) == len(required_tables),
        'existing_tables': existing_required_tables,
        'missing_tables': [table for table in required_tables if table not in existing_tables]
    }


def initialize_entity_cards_db(force_test_db=False):
    """Initialize entity cards tables."""
    try:
        logger.info("Creating entity cards tables...")
        
        session = get_session(force_test_db=force_test_db)
        engine = session.bind
        
        Base.metadata.create_all(
            engine,
            tables=[EntityCard.__table__, EntityCardUsage.__table__, EntityCardIndex.__table__],
            checkfirst=True,
        )
        session.close()
        
        logger.info("Entity cards tables initialized successfully.")
        
    except Exception as e:
        if "already exists" in str(e) or "DuplicateTable" in str(e):
            logger.info("Entity cards tables already exist. Skipping creation.")
        else:
            logger.error(f"Error initializing entity cards tables: {e}")
            raise


def drop_entity_cards_db():
    """Drop all entity cards tables."""
    session = get_session()
    engine = session.bind
    Base.metadata.drop_all(engine, tables=[
        EntityCardUsage.__table__, 
        EntityCardIndex.__table__, 
        EntityCard.__table__,
    ], checkfirst=True)
    session.close()
    logger.info("Entity cards tables dropped successfully.")


def reset_entity_cards_db():
    """Drop and recreate all entity cards tables."""
    logger.info("Resetting entity cards database...")
    drop_entity_cards_db()
    initialize_entity_cards_db()
    logger.info("Entity cards database reset completed.")


# Helper functions for entity card operations
def create_entity_card(session, entity_name, entity_type, summary, **kwargs):
    """
    Create a new entity card
    """
    entity_card = EntityCard(
        entity_name=entity_name,
        entity_type=entity_type,
        summary=summary,
        **kwargs
    )
    session.add(entity_card)
    return entity_card


def _normalize_index_term(term: str) -> str:
    """
    Normalize an index term for stable lookup.
    Current strategy: trim + lowercase + collapse internal whitespace.
    """
    if term is None:
        return ""
    s = str(term).strip().lower()
    if not s:
        return ""
    return " ".join(s.split())


def rebuild_entity_card_index_for_card(session, entity_card: EntityCard) -> None:
    """
    Rebuild the EntityCardIndex rows for a single card from canonical fields.
    This is intentionally simple and deterministic.
    """
    if not entity_card or not getattr(entity_card, "id", None):
        return

    # Delete existing index terms for this card.
    session.query(EntityCardIndex).filter(EntityCardIndex.entity_card_id == entity_card.id).delete(
        synchronize_session=False
    )

    # Collect terms.
    terms: list[tuple[str, str, float]] = []
    name_norm = _normalize_index_term(entity_card.entity_name)
    if name_norm:
        terms.append((name_norm, "name", 5.0))

    for alias in _ensure_list(entity_card.aliases):
        a = _normalize_index_term(alias)
        if a and a != name_norm:
            terms.append((a, "alias", 2.0))

    for alias in _ensure_list(entity_card.original_aliases):
        a = _normalize_index_term(alias)
        if a and a != name_norm:
            terms.append((a, "original_alias", 1.5))

    # De-dupe (keep highest priority).
    best: dict[str, tuple[str, float]] = {}
    for t, typ, pr in terms:
        cur = best.get(t)
        if cur is None or pr > cur[1]:
            best[t] = (typ, pr)

    for t, (typ, pr) in best.items():
        session.add(
            EntityCardIndex(
                entity_card_id=entity_card.id,
                index_term=t,
                term_type=typ,
                term_priority=pr,
            )
        )


def rebuild_entity_card_index(session, *, only_active: bool = True) -> int:
    """
    Rebuild the EntityCardIndex table from the current EntityCard rows.
    Returns number of cards indexed.
    """
    q = session.query(EntityCard)
    if only_active:
        q = q.filter(EntityCard.is_active == True)  # noqa: E712
    cards = q.all()
    for card in cards:
        rebuild_entity_card_index_for_card(session, card)
    session.commit()
    return len(cards)


def get_entity_card_by_name(session, entity_name):
    """
    Get entity card by entity name
    """
    return session.query(EntityCard).filter(
        EntityCard.entity_name == entity_name,
        EntityCard.is_active == True
    ).first()


def search_entity_cards(session, search_term, limit=10):
    """
    Search entity cards by name, aliases, or content
    """
    from sqlalchemy import or_, func, String

    term_norm = _normalize_index_term(search_term or "")
    if term_norm:
        # 1) Prefer the dedicated index table (exact/prefix match).
        # NOTE: use func.lower for safety in case index_term wasn't normalized historically.
        idx_subq = (
            session.query(EntityCardIndex.entity_card_id)
            .filter(func.lower(EntityCardIndex.index_term).like(f"{term_norm}%"))
            .order_by(EntityCardIndex.term_priority.desc())
            .limit(limit * 10)
            .subquery()
        )
        indexed_cards = (
            session.query(EntityCard)
            .join(idx_subq, EntityCard.id == idx_subq.c.entity_card_id)
            .filter(EntityCard.is_active == True)  # noqa: E712
            .order_by(EntityCard.usage_count.desc())
            .limit(limit)
            .all()
        )
        if indexed_cards:
            return indexed_cards

    # 2) Fallback: substring search across core text fields.
    return (
        session.query(EntityCard)
        .filter(
            EntityCard.is_active == True,  # noqa: E712
            or_(
                EntityCard.entity_name.ilike(f"%{search_term}%"),
                # SQLite: cast JSON to string for searching
                func.cast(EntityCard.aliases, String).ilike(f"%{search_term}%"),
                func.cast(EntityCard.original_aliases, String).ilike(f"%{search_term}%"),
                EntityCard.summary.ilike(f"%{search_term}%"),
                EntityCard.original_description.ilike(f"%{search_term}%"),
            ),
        )
        .limit(limit)
        .all()
    )


def update_entity_card_usage(session, entity_card_id, agent_name=None, session_id=None, query_text=None):
    """
    Update usage statistics for an entity card
    """
    # Atomic increment (avoid lost updates under concurrency).
    now = datetime.now(timezone.utc)
    updated = session.query(EntityCard).filter(EntityCard.id == entity_card_id).update(
        {
            EntityCard.usage_count: EntityCard.usage_count + 1,
            EntityCard.last_used: now,
        },
        synchronize_session=False,
    )
    if not updated:
        return None
    
    # Create usage record
    usage = EntityCardUsage(
        entity_card_id=entity_card_id,
        agent_name=agent_name,
        session_id=session_id,
        query_text=query_text
    )
    session.add(usage)
    return usage


def _format_entity_card_full(entity_card: "EntityCard") -> str:
    """Pure formatter for the full entity card. No DB writes."""
    parts = []
    parts.append(f"ENTITY CARD: {entity_card.entity_name}")
    parts.append(f"Type: {entity_card.entity_type}")
    parts.append("")

    # Original description
    parts.append("Original Description:")
    parts.append(entity_card.original_description or "No original description available")
    parts.append("")

    # Generated summary
    parts.append("Generated Summary:")
    parts.append(entity_card.summary or "")
    parts.append("")

    key_facts = _ensure_list(entity_card.key_facts)
    if key_facts:
        parts.append("Key Facts:")
        for fact in key_facts:
            parts.append(f"• {fact}")
        parts.append("")

    relationships = _ensure_list(entity_card.relationships)
    if relationships:
        parts.append("Key Relationships:")
        for rel in relationships:
            parts.append(f"• {rel}")
        parts.append("")

    original_aliases = _ensure_list(entity_card.original_aliases)
    if original_aliases:
        parts.append(f"Original Aliases: {', '.join(original_aliases)}")

    aliases = _ensure_list(entity_card.aliases)
    if aliases:
        parts.append(f"Processed Aliases: {', '.join(aliases)}")

    meta_dict = _coerce_card_metadata_to_dict(getattr(entity_card, "card_metadata", None))
    if meta_dict:
        parts.append("")
        parts.append("Metadata:")
        for k in sorted(meta_dict.keys(), key=lambda s: s.lower()):
            parts.append(f"• {k}: {meta_dict[k]}")

    return "\n".join(parts).strip()


def get_entity_cards_by_type(session, entity_type, limit=50):
    """
    Get entity cards by type
    """
    return session.query(EntityCard).filter(
        EntityCard.entity_type == entity_type,
        EntityCard.is_active == True
    ).order_by(EntityCard.usage_count.desc()).limit(limit).all()


def get_most_used_entity_cards(session, limit=20):
    """
    Get most frequently used entity cards
    """
    return session.query(EntityCard).filter(
        EntityCard.is_active == True
    ).order_by(EntityCard.usage_count.desc()).limit(limit).all()


def deactivate_entity_card(session, entity_name):
    """
    Deactivate an entity card (soft delete)
    """
    entity_card = get_entity_card_by_name(session, entity_name)
    if entity_card:
        entity_card.is_active = False
        return True
    return False


def get_entity_card_stats(session):
    """
    Get statistics about entity cards
    """
    total_cards = session.query(EntityCard).count()
    active_cards = session.query(EntityCard).filter(EntityCard.is_active == True).count()
    total_usage = session.query(func.sum(EntityCard.usage_count)).scalar() or 0
    
    # Get usage by type
    type_stats = session.query(
        EntityCard.entity_type,
        func.count(EntityCard.id).label('count'),
        func.sum(EntityCard.usage_count).label('total_usage')
    ).filter(EntityCard.is_active == True).group_by(EntityCard.entity_type).all()
    
    return {
        'total_cards': total_cards,
        'active_cards': active_cards,
        'total_usage': total_usage,
        'type_stats': [
            {
                'entity_type': stat.entity_type,
                'count': stat.count,
                'total_usage': stat.total_usage or 0
            }
            for stat in type_stats
        ]
    }


def get_entity_card_for_prompt_injection(session, entity_name) -> str:
    """
    Return a formatted string for prompt injection.
    Includes original description, summary, key facts, relationships, aliases, and ALL metadata.
    """
    entity_card = get_entity_card_by_name(session, entity_name)
    if not entity_card:
        return ""

    return _format_entity_card_full(entity_card)


def _coerce_card_metadata_to_dict(meta_raw) -> dict:
    """
    Normalize stored metadata into a dict.

    Accepts:
    - dict (new preferred)
    - list of {key,value} pairs (legacy agent output)
    - JSON string of either dict or list (legacy DB storage)
    """
    if not meta_raw:
        return {}

    loaded = meta_raw
    if isinstance(meta_raw, str):
        try:
            loaded = json.loads(meta_raw)
        except Exception:
            logger.debug("Could not parse entity card meta_raw as JSON", exc_info=True)
            return {}

    if isinstance(loaded, dict):
        return {str(k): loaded[k] for k in loaded.keys()}

    if isinstance(loaded, list):
        out = {}
        for item in loaded:
            if isinstance(item, dict) and "key" in item:
                out[str(item.get("key"))] = item.get("value")
        return out

    return {}


def _ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            logger.debug("Could not parse entity card list field as JSON", exc_info=True)
            return []
    return []


def _derive_level0_view(entity_card: "EntityCard") -> dict:
    """Fallback L0 view for cards without structured metadata.

    Falls back to the full summary verbatim when user_relevance_reason is
    missing — never truncates. Core entity memory in prompts must stay
    intact: a chopped summary loses meaning, breaks disambiguation, and
    can flip the planner's reasoning. If summary length is ever a real
    problem, fix it at write time (better summaries) or via a documented
    cap, not silently here.
    """
    urr = (getattr(entity_card, "user_relevance_reason", None) or "").strip()
    if not urr:
        urr = (entity_card.summary or "").strip()
    return {"user_relevance_reason": urr}


def _derive_level1_view(entity_card: "EntityCard") -> dict:
    """Fallback L1 view for cards without structured metadata.

    Same principle as L0: no silent truncation of the summary text.
    """
    summary = (entity_card.summary or "").strip()
    contact_info = _extract_contact_from_legacy_key_facts(entity_card)
    return {
        "summary": summary,
        "contact_info": contact_info,
        "key_facts": _ensure_list(entity_card.key_facts),
        "relationships": _ensure_list(entity_card.relationships),
    }


def _derive_level2_view(entity_card: "EntityCard") -> dict:
    """Fallback L2 view for cards without structured metadata."""
    contact_info = _extract_contact_from_legacy_key_facts(entity_card)
    return {
        "summary": (entity_card.summary or "").strip(),
        "contact_info": contact_info,
        "key_facts": _ensure_list(entity_card.key_facts),
        "relationships": _ensure_list(entity_card.relationships),
        "aliases": _ensure_list(entity_card.aliases),
        "original_aliases": _ensure_list(entity_card.original_aliases),
    }


def _extract_contact_from_legacy_key_facts(entity_card: "EntityCard") -> list[dict]:
    """
    For old cards that have contact info buried in key_facts,
    extract items that look like email/phone/carrier into structured form.
    """
    import re
    contact: list[dict] = []
    for fact in _ensure_list(entity_card.key_facts):
        f = str(fact).strip()
        fl = f.lower()
        if fl.startswith("email:") or fl.startswith("e-mail:"):
            val = f.split(":", 1)[1].strip()
            if val:
                contact.append({"type": "email", "value": val})
        elif fl.startswith("phone:") or fl.startswith("mobile:") or fl.startswith("cell:"):
            val = f.split(":", 1)[1].strip()
            if val:
                contact.append({"type": "phone", "value": val})
        elif fl.startswith("carrier:"):
            val = f.split(":", 1)[1].strip()
            if val:
                contact.append({"type": "carrier", "value": val})
        elif fl.startswith("address:"):
            val = f.split(":", 1)[1].strip()
            if val:
                contact.append({"type": "address", "value": val})
        elif re.search(r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b', f):
            contact.append({"type": "email", "value": f})
    return contact


def _format_contact_info(contact_items: list[dict]) -> list[str]:
    """Format structured contact_info into display lines."""
    lines: list[str] = []
    for ci in contact_items:
        if not isinstance(ci, dict):
            continue
        ctype = str(ci.get("type") or "").strip().capitalize()
        val = str(ci.get("value") or "").strip()
        label = str(ci.get("label") or "").strip()
        if not val:
            continue
        if label:
            lines.append(f"{ctype} ({label}): {val}")
        else:
            lines.append(f"{ctype}: {val}")
    return lines


def _get_view(entity_card: "EntityCard", level: int) -> dict:
    """Resolve the view dict for a given level, with fallbacks.

    The level hierarchy is:
      L0 – one-liner (user_relevance_reason)
      L1 – name + type + description/summary
      L2 – L1 + key facts
      L3 – L2 + contact info
      L4 – everything (relationships, aliases, metadata)

    Stored views in card_metadata follow the old 3-tier naming (level0/level1/level2).
    We map: L0→level0, L1-L3→level1, L4→level2.
    """
    meta = _coerce_card_metadata_to_dict(getattr(entity_card, "card_metadata", None))
    views = meta.get("views") if isinstance(meta.get("views"), dict) else {}

    if level == 0:
        v = views.get("level0")
        if isinstance(v, dict) and v.get("user_relevance_reason"):
            return v
        return _derive_level0_view(entity_card)

    if level <= 3:
        v = views.get("level1")
        if isinstance(v, dict) and v.get("summary"):
            return v
        return _derive_level1_view(entity_card)

    v = views.get("level2")
    if isinstance(v, dict) and v.get("summary"):
        return v
    return _derive_level2_view(entity_card)


def get_entity_card_for_prompt_injection_level(
    session,
    entity_name: str,
    *,
    level: int = 0,
) -> str:
    """
    Level-based prompt injection formatter.

    - level=0: one-liner (user_relevance_reason)
    - level=1: name + type + description/summary
    - level=2: L1 + key facts
    - level=3: L2 + contact info
    - level=4: everything (relationships, aliases)
    """
    entity_card = get_entity_card_by_name(session, entity_name)
    if not entity_card:
        return ""
    return _render_card_at_level(entity_card, level)


def get_entity_card_usage_report(
    session,
    *,
    limit: int = 25,
    since_days: int | None = None,
    entity_name: str | None = None,
) -> dict:
    """
    Convenience usage report for monitoring:
    - totals from EntityCard.usage_count
    - recent usage event counts from entity_card_usage
    """
    q_cards = session.query(EntityCard).filter(EntityCard.is_active == True)  # noqa: E712
    if entity_name:
        q_cards = q_cards.filter(EntityCard.entity_name == entity_name)

    total_cards = q_cards.count()
    total_injections = session.query(func.sum(EntityCard.usage_count)).filter(
        EntityCard.is_active == True  # noqa: E712
    ).scalar() or 0

    top_cards = (
        q_cards.order_by(EntityCard.usage_count.desc())
        .limit(limit)
        .all()
    )

    usage_q = session.query(EntityCardUsage)
    if since_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        usage_q = usage_q.filter(EntityCardUsage.created_at >= cutoff)
    if entity_name:
        # join to filter by name
        usage_q = usage_q.join(EntityCard, EntityCard.id == EntityCardUsage.entity_card_id).filter(
            EntityCard.entity_name == entity_name
        )

    recent_events = usage_q.count()

    return {
        "total_cards": total_cards,
        "total_injections_usage_count": int(total_injections),
        "recent_usage_events": int(recent_events),
        "top_cards": [
            {
                "entity_name": c.entity_name,
                "entity_type": c.entity_type,
                "usage_count": int(c.usage_count or 0),
                "last_used": c.last_used.isoformat() if c.last_used else None,
            }
            for c in top_cards
        ],
    }


def extract_entity_field(entity_card, field_name):
    """
    Extract a specific field from an entity card for granular injection.
    
    Args:
        entity_card: EntityCard instance
        field_name: One of: 'level_0', 'summary', 'key_facts', 'relationships', 'description',
                   'aliases', 'original_aliases', 'metadata', 'all'
    
    Returns:
        Formatted string for the requested field, or empty string if field doesn't exist
    """
    if not entity_card:
        return ""
    
    if field_name == "level_0":
        key_facts = _ensure_list(entity_card.key_facts)
        if key_facts:
            return "\n".join(f"• {fact}" for fact in key_facts[:2])
        return ""

    elif field_name == "summary":
        return entity_card.summary or ""
    
    elif field_name == "key_facts":
        key_facts = _ensure_list(entity_card.key_facts)
        if key_facts:
            return "\n".join(f"• {fact}" for fact in key_facts)
        return ""
    
    elif field_name == "relationships":
        relationships = _ensure_list(entity_card.relationships)
        if relationships:
            return "\n".join(f"• {rel}" for rel in relationships)
        return ""
    
    elif field_name == "description":
        return entity_card.original_description or ""
    
    elif field_name == "aliases":
        aliases = _ensure_list(entity_card.aliases)
        if aliases:
            return ", ".join(aliases)
        return ""
    
    elif field_name == "original_aliases":
        original_aliases = _ensure_list(entity_card.original_aliases)
        if original_aliases:
            return ", ".join(original_aliases)
        return ""
    
    elif field_name == "contact_info":
        meta_dict = _coerce_card_metadata_to_dict(getattr(entity_card, "card_metadata", None))
        contact_items = meta_dict.get("contact_info") if isinstance(meta_dict, dict) else []
        if not contact_items:
            view = _get_view(entity_card, 1)
            contact_items = view.get("contact_info") or []
        lines = _format_contact_info(contact_items)
        return "\n".join(lines) if lines else ""

    elif field_name == "metadata":
        meta_dict = _coerce_card_metadata_to_dict(getattr(entity_card, "card_metadata", None))
        
        if meta_dict and len(meta_dict) > 0:
            # Format as bullet points
            formatted = "\n".join(f"• {k}: {v}" for k, v in sorted(meta_dict.items(), key=lambda x: x[0].lower()))
            return formatted
        return ""
    
    elif field_name == "all":
        return _format_entity_card_full(entity_card)

    elif field_name == "type":
        return entity_card.entity_type or ""

    else:
        return ""


def _render_card_at_level(entity_card, level: int) -> str:
    """Shared renderer for entity card at a given view level.

    L0 – one-liner (user_relevance_reason)
    L1 – name + type + description/summary
    L2 – L1 + key facts
    L3 – L2 + contact info
    L4 – everything (relationships, aliases)
    """
    if not entity_card:
        return ""

    view = _get_view(entity_card, level)

    if level == 0:
        urr = (view.get("user_relevance_reason") or "").strip()
        if not urr:
            return ""
        return f"{entity_card.entity_name}: {urr}"

    parts: list[str] = []
    parts.append(f"{entity_card.entity_name} ({entity_card.entity_type})")

    if view.get("summary"):
        parts.append(view["summary"])

    if level >= 2:
        key_facts = view.get("key_facts") or []
        if isinstance(key_facts, list) and key_facts:
            parts.append("Facts:")
            for fact in key_facts:
                parts.append(f"• {fact}")

    if level >= 3:
        contact_lines = _format_contact_info(view.get("contact_info") or [])
        if contact_lines:
            parts.append("Contact:")
            for cl in contact_lines:
                parts.append(f"  {cl}")

    if level >= 4:
        relationships = view.get("relationships") or []
        if isinstance(relationships, list) and relationships:
            parts.append("Relationships:")
            for rel in relationships:
                parts.append(f"• {rel}")

        aliases = view.get("aliases") or []
        if isinstance(aliases, list) and aliases:
            parts.append(f"Aliases: {', '.join(str(a) for a in aliases)}")

    return "\n".join(parts).strip()


def extract_entity_field_leveled(entity_card, *, level: int = 1) -> str:
    """
    Render an entity card at the specified view level (0-4).

    Uses the same view-based rendering as
    ``get_entity_card_for_prompt_injection_level`` but accepts an already-loaded
    ``EntityCard`` instance, avoiding a second DB round-trip.
    """
    return _render_card_at_level(entity_card, level)


def get_entity_field_for_injection(session, entity_name, field_name):
    """
    Get a specific field from an entity card by entity name.
    
    Args:
        session: Database session
        entity_name: Name of the entity
        field_name: Field to extract (see extract_entity_field for options)
    
    Returns:
        Formatted string for the requested field, or empty string if not found
    """
    entity_card = get_entity_card_by_name(session, entity_name)
    if not entity_card:
        return ""
    
    return extract_entity_field(entity_card, field_name)


class EntityCardRunLog(Base):
    """
    Track entity card generation runs for incremental processing
    """
    __tablename__ = 'entity_card_run_log'
    
    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Run information
    last_run_time = Column(DateTime(timezone=True), nullable=False)
    nodes_processed = Column(Integer, nullable=False)
    nodes_updated = Column(Integer, nullable=False)  # How many entity cards were actually updated
    run_duration_seconds = Column(Float, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (
        Index('ix_entity_card_run_log_last_run_time', 'last_run_time'),
        Index('ix_entity_card_run_log_created_at', 'created_at'),
    )


def get_last_entity_card_run_time(session):
    """
    Get the timestamp of the last entity card generation run
    """
    last_run = session.query(EntityCardRunLog).order_by(EntityCardRunLog.last_run_time.desc()).first()
    return last_run.last_run_time if last_run else None


def track_entity_card_usage_best_effort(
    *,
    entity_name: str,
    agent_name: str | None = None,
    session_id: str | None = None,
    query_text: str | None = None,
) -> None:
    """
    Best-effort usage tracking that does NOT interfere with the caller's session/transaction.
    Uses a short-lived independent session, atomic increment, and inserts a usage event row.
    """
    session = get_session()
    try:
        entity_card = get_entity_card_by_name(session, entity_name)
        if not entity_card:
            return
        update_entity_card_usage(
            session,
            entity_card_id=entity_card.id,
            agent_name=agent_name,
            session_id=session_id,
            query_text=query_text,
        )
        session.commit()
    except Exception:
        try:
            session.rollback()
        except Exception:
            logger.debug("Session rollback failed", exc_info=True)
    finally:
        try:
            session.close()
        except Exception:
            logger.debug("Session close failed", exc_info=True)


def log_entity_card_run(session, last_run_time, nodes_processed, nodes_updated, run_duration_seconds=None):
    """
    Log an entity card generation run
    """
    run_log = EntityCardRunLog(
        last_run_time=last_run_time,
        nodes_processed=nodes_processed,
        nodes_updated=nodes_updated,
        run_duration_seconds=run_duration_seconds
    )
    session.add(run_log)
    session.commit()
    return run_log


class DescriptionRunLog(Base):
    """
    Track description creation runs for incremental processing
    """
    __tablename__ = 'description_run_log'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    last_run_time = Column(DateTime(timezone=True), nullable=False)
    nodes_processed = Column(Integer, nullable=False)
    nodes_updated = Column(Integer, nullable=False)
    run_duration_seconds = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    __table_args__ = (
        Index('ix_description_run_log_last_run_time', 'last_run_time'),
        Index('ix_description_run_log_created_at', 'created_at'),
    )


def get_last_description_run_time(session):
    """
    Get the timestamp of the last description creation run
    """
    last_run = session.query(DescriptionRunLog).order_by(DescriptionRunLog.last_run_time.desc()).first()
    return last_run.last_run_time if last_run else None

def log_description_run(session, last_run_time, nodes_processed, nodes_updated, run_duration_seconds=None):
    """
    Log a description creation run
    """
    run_log = DescriptionRunLog(
        last_run_time=last_run_time,
        nodes_processed=nodes_processed,
        nodes_updated=nodes_updated,
        run_duration_seconds=run_duration_seconds
    )
    session.add(run_log)
    session.commit()
    return run_log

def get_nodes_updated_since(session, since_timestamp):
    """
    Get nodes that have been updated since the given timestamp
    """
    from app.assistant.kg.db.knowledge_graph_db import Node
    return session.query(Node).filter(Node.updated_at > since_timestamp).all()
