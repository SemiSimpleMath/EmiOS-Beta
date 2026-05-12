"""Entity Cards V2 — viewer + CRUD for non-kg cards.

Reads entity_card_v2 / entity_card_section / entity_card_bullet.

kg cards (entity_node_id IS NOT NULL) are read-only — they're driven by
the build pipeline.

Non-kg cards (entity_node_id IS NULL) are user-authored and editable
here: POST creates, PUT edits, DELETE removes. Same retrieval surface
(card_title + card_aliases) serves both.
"""
import uuid
from typing import Optional

from flask import Blueprint, render_template, jsonify, request

from app.models.base import get_session
from app.assistant.entity_management.entity_card_v2 import (
    EntityCardV2, EntityCardSection, EntityCardBullet,
    LEVEL_0, SUMMARY,
)
from app.assistant.entity_management.entity_catalog import EntityCatalog
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now

entity_cards_v2_bp = Blueprint('entity_cards_v2', __name__)
logger = get_logger(__name__)


# --- helpers ----------------------------------------------------------------

def _normalize_aliases(raw) -> list[str]:
    """Accept a list or comma-separated string; return de-duplicated list of
    non-empty trimmed strings preserving caller order."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(',')]
    elif isinstance(raw, list):
        parts = [str(p).strip() for p in raw]
    else:
        parts = []
    seen = set()
    out = []
    for p in parts:
        if not p:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _serialize_card(session, card: EntityCardV2) -> dict:
    """Full card payload used by both GET-one and POST/PUT responses."""
    node = session.get(Node, card.entity_node_id) if card.entity_node_id else None
    sections = session.query(EntityCardSection).filter(
        EntityCardSection.card_id == card.id
    ).order_by(EntityCardSection.position).all()
    out_sections = []
    for s in sections:
        bullets = session.query(EntityCardBullet).filter(
            EntityCardBullet.section_id == s.id
        ).order_by(EntityCardBullet.position).all()
        out_sections.append({
            'id': s.id,
            'section_name': s.section_name,
            'position': s.position,
            'intro_text': s.intro_text,
            'bullets': [
                {
                    'id': b.id,
                    'bullet_text': b.bullet_text,
                    'confidence_tier': b.confidence_tier,
                    'source_node_ids': b.source_node_ids or [],
                    'source_edge_ids': b.source_edge_ids or [],
                }
                for b in bullets
            ],
        })
    return {
        'id': card.id,
        'entity_node_id': card.entity_node_id,
        'is_kg_card': card.entity_node_id is not None,
        'card_title': card.card_title,
        'card_aliases': card.card_aliases or [],
        'entity_label': card.card_title or (node.label if node else '(untitled)'),
        'entity_type': card.entity_type,
        'entity_category': node.category if node else None,
        'last_built_at': card.last_built_at.isoformat() if card.last_built_at else None,
        'sections': out_sections,
    }


def _upsert_summary_section(session, card: EntityCardV2, content: str) -> None:
    """Replace (or create) the SUMMARY section so its intro_text = content.

    Non-kg cards live entirely as level_0 + summary sections; this helper
    handles the summary side. _upsert_level_0_section handles level_0.
    """
    existing = session.query(EntityCardSection).filter(
        EntityCardSection.card_id == card.id,
        EntityCardSection.section_name == SUMMARY,
    ).first()
    if existing is None:
        section = EntityCardSection(
            id=str(uuid.uuid4()),
            card_id=card.id,
            section_name=SUMMARY,
            position=1,
            intro_text=content,
        )
        session.add(section)
    else:
        existing.intro_text = content


def _upsert_level_0_section(session, card: EntityCardV2, one_liner: str) -> None:
    """Replace (or create) the level_0 section so the L0 renderer has a
    one-liner to surface. Used by non-kg cards which don't go through the
    LLM-driven level_0 builder."""
    existing = session.query(EntityCardSection).filter(
        EntityCardSection.card_id == card.id,
        EntityCardSection.section_name == LEVEL_0,
    ).first()
    if existing is None:
        section = EntityCardSection(
            id=str(uuid.uuid4()),
            card_id=card.id,
            section_name=LEVEL_0,
            position=0,
            intro_text=one_liner,
        )
        session.add(section)
    else:
        existing.intro_text = one_liner


def _derive_one_liner(content: str) -> str:
    """First sentence (or first ~100 chars) of content for L0 display."""
    text = (content or "").strip()
    if not text:
        return ""
    # First sentence terminator
    for terminator in ['. ', '! ', '? ', '\n']:
        idx = text.find(terminator)
        if 0 < idx < 200:
            return text[: idx + 1].strip()
    return text[:160].strip()


def _reload_catalog() -> None:
    """Invalidate the in-memory catalog so a freshly created/edited/deleted
    card surfaces in entity detection on the next prompt build."""
    try:
        EntityCatalog.instance().reload_from_db()
    except Exception as e:
        logger.warning("entity_cards_v2: catalog reload failed: %s", e)


# --- routes -----------------------------------------------------------------

@entity_cards_v2_bp.route('/entity-cards-v2', methods=['GET'])
def entity_cards_v2_page():
    return render_template('entity_cards_v2.html')


@entity_cards_v2_bp.route('/api/entity-cards-v2', methods=['GET'])
def api_list_cards():
    """List all v2 cards with basic metadata. Used by the index pane."""
    session = get_session()
    try:
        cards = session.query(EntityCardV2).filter(EntityCardV2.is_active == True).all()
        out = []
        for c in cards:
            node = session.get(Node, c.entity_node_id) if c.entity_node_id else None
            section_count = session.query(EntityCardSection).filter(
                EntityCardSection.card_id == c.id
            ).count()
            bullet_count = session.query(EntityCardBullet).join(
                EntityCardSection, EntityCardBullet.section_id == EntityCardSection.id
            ).filter(EntityCardSection.card_id == c.id).count()
            out.append({
                'id': c.id,
                'entity_node_id': c.entity_node_id,
                'is_kg_card': c.entity_node_id is not None,
                'card_title': c.card_title,
                'card_aliases': c.card_aliases or [],
                'entity_label': c.card_title or (node.label if node else '(untitled)'),
                'entity_type': c.entity_type,
                'entity_category': node.category if node else None,
                'last_built_at': c.last_built_at.isoformat() if c.last_built_at else None,
                'sections': section_count,
                'bullets': bullet_count,
            })
        out.sort(key=lambda d: (d['entity_label'] or '').lower())
        return jsonify({'cards': out, 'total': len(out)})
    finally:
        session.close()


@entity_cards_v2_bp.route('/api/entity-cards-v2/<card_id>', methods=['GET'])
def api_get_card(card_id: str):
    """Return one card's full structure: sections + bullets in display order."""
    session = get_session()
    try:
        card = session.get(EntityCardV2, card_id)
        if card is None:
            return jsonify({'error': 'not found'}), 404
        return jsonify(_serialize_card(session, card))
    finally:
        session.close()


@entity_cards_v2_bp.route('/api/entity-cards-v2', methods=['POST'])
def api_create_non_kg_card():
    """Create a non-kg card (user-authored). Required body:
        { "title": str, "aliases": [str] or "a,b,c", "content": str }

    Title must be unique across active cards (kg or non-kg) — the catalog
    is keyed on it. Returns the full serialized card on success.
    """
    body = request.get_json(silent=True) or {}
    title = (body.get('title') or '').strip()
    content = (body.get('content') or '').strip()
    aliases = _normalize_aliases(body.get('aliases'))

    if not title:
        return jsonify({'error': 'title is required'}), 400
    if not content:
        return jsonify({'error': 'content is required'}), 400

    session = get_session()
    try:
        clash = session.query(EntityCardV2).filter(
            EntityCardV2.card_title == title,
            EntityCardV2.is_active == True,  # noqa: E712
        ).first()
        if clash is not None:
            return jsonify({
                'error': f'a card titled {title!r} already exists',
                'existing_id': clash.id,
            }), 409

        card = EntityCardV2(
            id=str(uuid.uuid4()),
            entity_node_id=None,
            entity_type='Manual',
            is_active=True,
            card_title=title,
            card_aliases=aliases or None,
        )
        session.add(card)
        session.flush()

        _upsert_level_0_section(session, card, _derive_one_liner(content))
        _upsert_summary_section(session, card, content)

        card.last_built_at = utc_now()
        session.commit()
        _reload_catalog()
        return jsonify(_serialize_card(session, card)), 201
    except Exception as e:
        session.rollback()
        logger.error("api_create_non_kg_card failed: %s", e, exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@entity_cards_v2_bp.route('/api/entity-cards-v2/<card_id>', methods=['PUT'])
def api_edit_non_kg_card(card_id: str):
    """Edit a non-kg card. Body fields are all optional; only what's
    present gets updated:
        { "title": str, "aliases": [str] or "a,b,c", "content": str }

    kg cards are rejected — they're driven by the build pipeline. (Per-field
    editing of kg cards is phase 2.)
    """
    body = request.get_json(silent=True) or {}
    session = get_session()
    try:
        card = session.get(EntityCardV2, card_id)
        if card is None:
            return jsonify({'error': 'not found'}), 404
        if card.entity_node_id is not None:
            return jsonify({
                'error': 'kg cards are not user-editable yet; '
                         'edit the underlying KG node or wait for phase 2'
            }), 403

        if 'title' in body:
            new_title = (body.get('title') or '').strip()
            if not new_title:
                return jsonify({'error': 'title cannot be empty'}), 400
            if new_title != card.card_title:
                clash = session.query(EntityCardV2).filter(
                    EntityCardV2.card_title == new_title,
                    EntityCardV2.is_active == True,  # noqa: E712
                    EntityCardV2.id != card.id,
                ).first()
                if clash is not None:
                    return jsonify({'error': f'a card titled {new_title!r} already exists'}), 409
                card.card_title = new_title

        if 'aliases' in body:
            card.card_aliases = _normalize_aliases(body.get('aliases')) or None

        if 'content' in body:
            content = (body.get('content') or '').strip()
            if not content:
                return jsonify({'error': 'content cannot be empty'}), 400
            _upsert_level_0_section(session, card, _derive_one_liner(content))
            _upsert_summary_section(session, card, content)

        card.last_built_at = utc_now()
        session.commit()
        _reload_catalog()
        return jsonify(_serialize_card(session, card))
    except Exception as e:
        session.rollback()
        logger.error("api_edit_non_kg_card failed: %s", e, exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()


@entity_cards_v2_bp.route('/api/entity-cards-v2/<card_id>', methods=['DELETE'])
def api_delete_non_kg_card(card_id: str):
    """Delete a non-kg card. kg cards are rejected — to remove a kg card,
    mark its underlying KG node inactive."""
    session = get_session()
    try:
        card = session.get(EntityCardV2, card_id)
        if card is None:
            return jsonify({'error': 'not found'}), 404
        if card.entity_node_id is not None:
            return jsonify({'error': 'kg cards cannot be deleted here'}), 403
        session.delete(card)
        session.commit()
        _reload_catalog()
        return jsonify({'ok': True})
    except Exception as e:
        session.rollback()
        logger.error("api_delete_non_kg_card failed: %s", e, exc_info=True)
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()
