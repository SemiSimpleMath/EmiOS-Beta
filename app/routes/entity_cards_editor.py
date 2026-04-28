"""
Entity Cards Editor Route
Flask blueprint for editing and managing entity cards
"""
from flask import Blueprint, render_template, request, jsonify
from app.models.base import get_session
from app.assistant.entity_management.entity_cards import (
    EntityCard,
    get_entity_card_by_name,
    search_entity_cards,
    get_entity_cards_by_type,
    get_most_used_entity_cards,
    get_entity_card_stats,
    create_entity_card,
    deactivate_entity_card,
    rebuild_entity_card_index_for_card,
)
import sqlalchemy
from sqlalchemy import func
import uuid
from datetime import datetime, timezone
from app.assistant.utils.logging_config import get_logger

entity_cards_editor_bp = Blueprint('entity_cards_editor', __name__)
logger = get_logger(__name__)


class EntityCardManager:
    """Manager for entity card operations"""
    
    def __init__(self, session=None):
        self.session = session or get_session()
    
    def get_all_entity_cards(self, limit=100, offset=0, search_term=None, entity_type=None, sort_by='name', include_inactive=False):
        """Get entity cards with filtering and sorting.

        When a search_term is provided, results are ranked:
          1. Exact name match
          2. Name starts with search term
          3. Name contains search term
          4. Alias match
          5. Summary/type contain search term

        ``include_inactive`` brings deactivated cards into the listing — used
        by the editor's "Show inactive" toggle so the user can re-activate them.
        """
        from sqlalchemy import or_, case

        query = self.session.query(EntityCard)
        if not include_inactive:
            query = query.filter(EntityCard.is_active == True)

        if entity_type:
            query = query.filter(EntityCard.entity_type == entity_type)

        if search_term:
            term_lower = search_term.strip().lower()
            term_pattern = f'%{search_term}%'
            starts_pattern = f'{search_term}%'

            # aliases/original_aliases are stored as JSON text, so cast to
            # string for LIKE matching.
            aliases_str = func.cast(EntityCard.aliases, sqlalchemy.String)
            orig_aliases_str = func.cast(EntityCard.original_aliases, sqlalchemy.String)

            query = query.filter(
                or_(
                    EntityCard.entity_name.ilike(term_pattern),
                    aliases_str.ilike(term_pattern),
                    orig_aliases_str.ilike(term_pattern),
                    EntityCard.summary.ilike(term_pattern),
                    EntityCard.entity_type.ilike(term_pattern),
                )
            )

            relevance = case(
                (func.lower(EntityCard.entity_name) == term_lower, 0),
                (EntityCard.entity_name.ilike(starts_pattern), 1),
                (EntityCard.entity_name.ilike(term_pattern), 2),
                (aliases_str.ilike(term_pattern), 3),
                (orig_aliases_str.ilike(term_pattern), 3),
                else_=4,
            )
            query = query.order_by(relevance, EntityCard.entity_name.asc())
        else:
            if sort_by == 'name':
                query = query.order_by(EntityCard.entity_name.asc())
            elif sort_by == 'usage':
                query = query.order_by(EntityCard.usage_count.desc())
            elif sort_by == 'created':
                query = query.order_by(EntityCard.created_at.desc())
            elif sort_by == 'updated':
                query = query.order_by(EntityCard.updated_at.desc())

        total = query.count()
        cards = query.offset(offset).limit(limit).all()

        return {
            'cards': [self._card_to_dict(card) for card in cards],
            'total': total,
            'limit': limit,
            'offset': offset
        }
    
    def get_entity_card_by_id(self, card_id):
        """Get entity card by ID (SQLite uses string IDs)"""
        try:
            # For SQLite, IDs are stored as strings, so query with string directly
            card = self.session.query(EntityCard).filter(EntityCard.id == str(card_id)).first()
            if card:
                return {'success': True, 'card': self._card_to_dict(card)}
            return {'success': False, 'message': 'Entity card not found'}
        except Exception as e:
            return {'success': False, 'message': f'Error retrieving card: {str(e)}'}
    
    def create_entity_card(self, data):
        """Create a new entity card"""
        try:
            # Check if entity name already exists
            existing = get_entity_card_by_name(self.session, data.get('entity_name'))
            if existing:
                return {'success': False, 'message': f'Entity card with name "{data["entity_name"]}" already exists'}
            
            card = EntityCard(
                entity_name=data.get('entity_name'),
                entity_type=data.get('entity_type', 'unknown'),
                summary=data.get('summary', ''),
                key_facts=data.get('key_facts', []),
                relationships=data.get('relationships', []),
                aliases=data.get('aliases', []),
                original_description=data.get('original_description'),
                original_aliases=data.get('original_aliases', []),
                confidence=data.get('confidence'),
                source_node_id=str(data['source_node_id']) if data.get('source_node_id') else None,
                card_metadata=data.get('card_metadata', {})
            )
            
            self.session.add(card)
            self.session.flush()  # populate card.id before the index rebuild reads it
            # Manually-created cards must populate entity_card_index too — otherwise
            # the alias-lookup path used by prompt injection can't find them until
            # something else (a regen, a v2 pipeline run) touches the row.
            rebuild_entity_card_index_for_card(self.session, card)
            self.session.commit()

            return {'success': True, 'card': self._card_to_dict(card)}
        except Exception as e:
            self.session.rollback()
            return {'success': False, 'message': f'Error creating entity card: {str(e)}'}
    
    def update_entity_card(self, card_id, data):
        """Update an existing entity card (SQLite uses string IDs)"""
        try:
            card = self.session.query(EntityCard).filter(EntityCard.id == str(card_id)).first()
            
            if not card:
                return {'success': False, 'message': 'Entity card not found'}
            
            # Update fields
            if 'entity_name' in data:
                # Check if new name conflicts with another card
                existing = get_entity_card_by_name(self.session, data['entity_name'])
                if existing and existing.id != card.id:
                    return {'success': False, 'message': f'Entity card with name "{data["entity_name"]}" already exists'}
                card.entity_name = data['entity_name']
            
            if 'entity_type' in data:
                card.entity_type = data['entity_type']
            if 'user_relevance_reason' in data:
                card.user_relevance_reason = data['user_relevance_reason']
                self._sync_views_urr(card, data['user_relevance_reason'])
            if 'summary' in data:
                card.summary = data['summary']
            if 'key_facts' in data:
                card.key_facts = data['key_facts']
            if 'relationships' in data:
                card.relationships = data['relationships']
            if 'aliases' in data:
                card.aliases = data['aliases']
            if 'original_description' in data:
                card.original_description = data['original_description']
            if 'original_aliases' in data:
                card.original_aliases = data['original_aliases']
            if 'confidence' in data:
                card.confidence = data['confidence']
            if 'source_node_id' in data:
                card.source_node_id = str(data['source_node_id']) if data.get('source_node_id') else None
            if 'card_metadata' in data:
                card.card_metadata = data['card_metadata']
            if 'is_active' in data:
                # Re-enable an explicitly deactivated card. Setting True
                # makes it eligible for prompt injection again; setting False
                # is the same as the soft delete in delete_entity_card.
                card.is_active = bool(data['is_active'])

            card.updated_at = datetime.now(timezone.utc)

            # Rebuild the alias index whenever a name/alias-shaped field
            # changed — otherwise the alias-lookup path that drives prompt
            # injection won't see the edit until the next pipeline run.
            if any(k in data for k in (
                "entity_name", "aliases", "original_aliases", "is_active"
            )):
                rebuild_entity_card_index_for_card(self.session, card)
            self.session.commit()

            return {'success': True, 'card': self._card_to_dict(card)}
        except Exception as e:
            self.session.rollback()
            return {'success': False, 'message': f'Error updating entity card: {str(e)}'}
    
    def delete_entity_card(self, card_id):
        """Soft delete an entity card (SQLite uses string IDs)"""
        try:
            card = self.session.query(EntityCard).filter(EntityCard.id == str(card_id)).first()
            
            if not card:
                return {'success': False, 'message': 'Entity card not found'}
            
            card.is_active = False
            card.updated_at = datetime.now(timezone.utc)
            self.session.commit()
            
            return {'success': True, 'message': 'Entity card deleted successfully'}
        except Exception as e:
            self.session.rollback()
            return {'success': False, 'message': f'Error deleting entity card: {str(e)}'}
    
    def get_entity_types(self):
        """Get list of all entity types"""
        types = self.session.query(EntityCard.entity_type).filter(
            EntityCard.is_active == True
        ).distinct().all()
        return [t[0] for t in types]
    
    @staticmethod
    def _sync_views_urr(card, new_urr: str):
        """Keep card_metadata.views.level*.user_relevance_reason in sync with the column."""
        import json as _json
        from sqlalchemy.orm.attributes import flag_modified
        meta = card.card_metadata
        if isinstance(meta, str):
            meta = _json.loads(meta)
        if not isinstance(meta, dict):
            return
        views = meta.get("views")
        if not isinstance(views, dict):
            return
        changed = False
        for level_key in ("level0", "level1", "level2"):
            lv = views.get(level_key)
            if isinstance(lv, dict) and "user_relevance_reason" in lv:
                lv["user_relevance_reason"] = new_urr
                changed = True
        if changed:
            card.card_metadata = meta
            flag_modified(card, "card_metadata")

    def _card_to_dict(self, card):
        """Convert EntityCard to dictionary"""
        return {
            'id': str(card.id),
            'entity_name': card.entity_name,
            'entity_type': card.entity_type,
            'user_relevance_reason': card.user_relevance_reason,
            'summary': card.summary,
            'key_facts': card.key_facts or [],
            'relationships': card.relationships or [],
            'aliases': card.aliases or [],
            'original_description': card.original_description,
            'original_aliases': card.original_aliases or [],
            'confidence': card.confidence,
            'source_node_id': str(card.source_node_id) if card.source_node_id else None,
            'card_metadata': card.card_metadata or {},
            'is_active': card.is_active,
            'usage_count': card.usage_count,
            'last_used': card.last_used.isoformat() if card.last_used else None,
            'created_at': card.created_at.isoformat() if card.created_at else None,
            'updated_at': card.updated_at.isoformat() if card.updated_at else None
        }


def get_manager(session=None):
    """Get EntityCardManager instance with proper session handling"""
    if session is None:
        session = get_session()
    return EntityCardManager(session=session)


@entity_cards_editor_bp.route('/entity_cards', methods=['GET'])
def entity_cards_page():
    """Main entity cards editor page"""
    return render_template('entity_cards_editor.html')


@entity_cards_editor_bp.route('/api/entity_cards', methods=['GET'])
def api_get_entity_cards():
    """Get entity cards with filtering and pagination"""
    session = get_session()
    try:
        manager = get_manager(session=session)
        limit = request.args.get('limit', 50, type=int)
        offset = request.args.get('offset', 0, type=int)
        search_term = request.args.get('search', None)
        entity_type = request.args.get('type', None)
        sort_by = request.args.get('sort', 'name')
        include_inactive = request.args.get('include_inactive', '0').lower() in ('1', 'true', 'yes')

        result = manager.get_all_entity_cards(
            limit=limit,
            offset=offset,
            search_term=search_term,
            entity_type=entity_type,
            sort_by=sort_by,
            include_inactive=include_inactive,
        )
        
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        session.close()


@entity_cards_editor_bp.route('/api/entity_cards/<card_id>', methods=['GET'])
def api_get_entity_card(card_id):
    """Get a specific entity card by ID"""
    logger.debug("api_get_entity_card called for: %s", card_id)
    session = get_session()
    try:
        manager = get_manager(session=session)
        result = manager.get_entity_card_by_id(card_id)
        if result['success']:
            return jsonify(result)
        return jsonify(result), 404
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        session.close()


@entity_cards_editor_bp.route('/api/entity_cards', methods=['POST'])
def api_create_entity_card():
    """Create a new entity card"""
    session = get_session()
    try:
        manager = get_manager(session=session)
        data = request.get_json()
        result = manager.create_entity_card(data)
        if result['success']:
            return jsonify(result), 201
        return jsonify(result), 400
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        session.close()


@entity_cards_editor_bp.route('/api/entity_cards/<card_id>', methods=['PUT'])
def api_update_entity_card(card_id):
    """Update an existing entity card"""
    session = get_session()
    try:
        manager = get_manager(session=session)
        data = request.get_json()
        result = manager.update_entity_card(card_id, data)
        if result['success']:
            return jsonify(result)
        return jsonify(result), 400
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        session.close()


@entity_cards_editor_bp.route('/api/entity_cards/<card_id>', methods=['DELETE'])
def api_delete_entity_card(card_id):
    """Delete an entity card"""
    session = get_session()
    try:
        manager = get_manager(session=session)
        result = manager.delete_entity_card(card_id)
        if result['success']:
            return jsonify(result)
        return jsonify(result), 404
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        session.close()


@entity_cards_editor_bp.route('/api/entity_cards/stats', methods=['GET'])
def api_get_stats():
    """Get entity card statistics"""
    session = get_session()
    try:
        stats = get_entity_card_stats(session)
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        session.close()


@entity_cards_editor_bp.route('/api/entity_cards/types', methods=['GET'])
def api_get_types():
    """Get list of all entity types"""
    session = get_session()
    try:
        manager = get_manager(session=session)
        types = manager.get_entity_types()
        return jsonify({'success': True, 'types': types})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        session.close()

