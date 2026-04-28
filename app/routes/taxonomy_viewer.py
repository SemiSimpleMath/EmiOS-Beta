"""
Taxonomy Web Viewer Blueprint
Integrates the taxonomy reviewer into the main Flask app
"""
from flask import Blueprint, render_template, request, jsonify, g
from sqlalchemy import and_

from app.models.base import get_session
from app.assistant.utils.logging_config import get_logger
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg_core.taxonomy.models import (
    Taxonomy, NodeTaxonomyLink
)
from app.assistant.kg_core.taxonomy.manager import TaxonomyManager

logger = get_logger(__name__)

# Create blueprint with custom template and static folders
taxonomy_viewer_bp = Blueprint(
    'taxonomy_viewer',
    __name__,
    template_folder='../assistant/kg_core/taxonomy/templates',
    static_folder='../assistant/kg_core/taxonomy/static',
    static_url_path='/taxonomy_webviewer/static'
)


# Session management
@taxonomy_viewer_bp.before_request
def before_request():
    """Initialize session tracking for this request"""
    g.sessions = []


@taxonomy_viewer_bp.teardown_request
def teardown_request(exception=None):
    """Close all sessions created during this request"""
    sessions = getattr(g, 'sessions', [])
    for session in sessions:
        try:
            session.close()
        except Exception as e:
            logger.debug(f"taxonomy_viewer: session.close() failed during teardown: {e}", exc_info=True)


def get_tracked_session():
    """Get a session that will be automatically closed"""
    session = get_session()
    if hasattr(g, 'sessions'):
        g.sessions.append(session)
    return session


# Import TaxonomyReviewManager from the standalone app
from app.assistant.kg_core.taxonomy.reviewer_web import TaxonomyReviewManager


# Main page route
@taxonomy_viewer_bp.route('/taxonomy_webviewer')
def index():
    """Serve the taxonomy reviewer interface"""
    return render_template('taxonomy_reviewer.html')


# API Routes
@taxonomy_viewer_bp.route('/api/taxonomy/tree')
def api_taxonomy_tree():
    """Get the full taxonomy tree"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    return jsonify(manager.get_taxonomy_tree())


@taxonomy_viewer_bp.route('/api/taxonomy/statistics')
def api_statistics():
    """Get taxonomy statistics"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    return jsonify(manager.get_statistics())


@taxonomy_viewer_bp.route('/api/taxonomy/suggestions')
def api_suggestions():
    """Get pending taxonomy suggestions"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    return jsonify(manager.get_pending_suggestions())


@taxonomy_viewer_bp.route('/api/taxonomy/reviews')
def api_reviews():
    """Get pending node classification reviews"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    return jsonify(manager.get_pending_node_reviews())


@taxonomy_viewer_bp.route('/api/taxonomy/suggestions/<int:suggestion_id>/approve', methods=['POST'])
def api_approve_suggestion(suggestion_id):
    """Approve a taxonomy suggestion"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    result = manager.approve_suggestion(suggestion_id)
    return jsonify(result)


@taxonomy_viewer_bp.route('/api/taxonomy/suggestions/approve_all', methods=['POST'])
def api_approve_all_suggestions():
    """Approve all pending taxonomy suggestions"""
    try:
        session = get_tracked_session()
        manager = TaxonomyReviewManager(session)
        
        # Get all pending suggestions
        result = manager.get_pending_suggestions()
        
        if not result.get('success'):
            return jsonify({
                "success": False,
                "message": result.get('message', 'Failed to fetch suggestions')
            })
        
        suggestions = result.get('suggestions', [])
        
        if not suggestions:
            return jsonify({
                "success": True,
                "message": "No pending suggestions to approve",
                "approved_count": 0
            })
        
        # Approve each suggestion
        approved_count = 0
        failed_count = 0
        errors = []
        
        for suggestion in suggestions:
            try:
                approve_result = manager.approve_suggestion(suggestion['id'])
                if approve_result.get('success'):
                    approved_count += 1
                else:
                    failed_count += 1
                    errors.append(f"Suggestion {suggestion['id']}: {approve_result.get('message', 'Unknown error')}")
            except Exception as e:
                failed_count += 1
                errors.append(f"Suggestion {suggestion['id']}: {str(e)}")
        
        if failed_count == 0:
            return jsonify({
                "success": True,
                "message": f"Successfully approved all {approved_count} suggestions",
                "approved_count": approved_count
            })
        else:
            return jsonify({
                "success": approved_count > 0,
                "message": f"Approved {approved_count} suggestions, {failed_count} failed",
                "approved_count": approved_count,
                "failed_count": failed_count,
                "errors": errors[:5]
            })
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "message": f"Server error: {str(e)}"
        }), 500


@taxonomy_viewer_bp.route('/api/taxonomy/suggestions/<int:suggestion_id>/reject', methods=['POST'])
def api_reject_suggestion(suggestion_id):
    """Reject a taxonomy suggestion"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    result = manager.reject_suggestion(suggestion_id)
    return jsonify(result)


@taxonomy_viewer_bp.route('/api/taxonomy/reviews/<int:review_id>/approve', methods=['POST'])
def api_approve_review(review_id):
    """Approve a node classification review"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    result = manager.approve_node_review(review_id)
    return jsonify(result)


@taxonomy_viewer_bp.route('/api/taxonomy/reviews/<int:review_id>/reject', methods=['POST'])
def api_reject_review(review_id):
    """Reject a node classification review"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    result = manager.reject_node_review(review_id)
    return jsonify(result)


@taxonomy_viewer_bp.route('/api/taxonomy/reviews/<int:review_id>/accept-proposer', methods=['POST'])
def api_accept_proposer_review(review_id):
    """Accept proposer's suggestion for a review"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    result = manager.accept_proposer_review(review_id)
    return jsonify(result)


@taxonomy_viewer_bp.route('/api/taxonomy/cleanup-suggestions', methods=['POST'])
def api_cleanup_suggestions():
    """Clean up obsolete taxonomy suggestions"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    result = manager.cleanup_approved_suggestions()
    return jsonify(result)


@taxonomy_viewer_bp.route('/api/taxonomy/edit/<int:node_id>', methods=['POST'])
def api_edit_taxonomy(node_id):
    """Edit a taxonomy node"""
    data = request.get_json()
    new_label = data.get('label')
    new_description = data.get('description')
    
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    result = manager.edit_taxonomy(node_id, new_label, None, new_description)
    return jsonify(result)


@taxonomy_viewer_bp.route('/api/taxonomy/delete/<int:node_id>', methods=['DELETE'])
def api_delete_taxonomy(node_id):
    """Delete a taxonomy node"""
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    result = manager.delete_taxonomy(node_id)
    return jsonify(result)


@taxonomy_viewer_bp.route('/api/taxonomy/export')
def api_export_taxonomy():
    """Export the entire taxonomy as JSON"""
    from app.assistant.kg_core.taxonomy.export import TaxonomyExporter
    from datetime import datetime
    
    export_format = request.args.get('format', 'tree')  # tree, flat, or paths
    
    try:
        session = get_tracked_session()
        exporter = TaxonomyExporter(session)
        
        if export_format == 'tree':
            data = exporter.export_to_dict()
        elif export_format == 'flat':
            data = {
                'metadata': {
                    'exported_at': datetime.utcnow().isoformat(),
                    'format': 'flat',
                    'version': '1.0'
                },
                'nodes': exporter.export_flat_list()
            }
        elif export_format == 'paths':
            data = {
                'metadata': {
                    'exported_at': datetime.utcnow().isoformat(),
                    'format': 'paths',
                    'version': '1.0'
                },
                'paths': exporter.export_paths()
            }
        else:
            return jsonify({
                'success': False,
                'message': f'Invalid format: {export_format}'
            }), 400
        
        return jsonify(data)
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'message': f'Export error: {str(e)}'
        }), 500


@taxonomy_viewer_bp.route('/api/taxonomy/add', methods=['POST'])
def api_add_taxonomy():
    """Add a new taxonomy category"""
    data = request.get_json()
    parent_id = data.get('parent_id')
    label = data.get('label')
    
    session = get_tracked_session()
    manager = TaxonomyReviewManager(session)
    result = manager.add_taxonomy_child(parent_id, label)
    return jsonify(result)


@taxonomy_viewer_bp.route('/api/taxonomy/search-nodes')
def api_search_nodes():
    """Search for nodes by label or UUID"""
    import uuid as uuid_module
    query = request.args.get('query', '').strip()
    
    if not query:
        return jsonify({"success": False, "message": "Query parameter required"}), 400
    
    try:
        session = get_tracked_session()
        
        # Try to parse as UUID first
        try:
            node_uuid = uuid_module.UUID(query)
            nodes = session.query(Node).filter(Node.id == node_uuid).all()
        except ValueError:
            # Not a UUID, search by label (case-insensitive, partial match)
            nodes = session.query(Node).filter(
                Node.label.ilike(f'%{query}%')
            ).limit(50).all()
        
        results = []
        for node in nodes:
            # Get taxonomy classifications
            tax_links = session.query(NodeTaxonomyLink, Taxonomy).join(
                Taxonomy, NodeTaxonomyLink.taxonomy_id == Taxonomy.id
            ).filter(
                NodeTaxonomyLink.node_id == node.id
            ).all()
            
            taxonomies = []
            for link, tax in tax_links:
                # Get full path
                tax_manager = TaxonomyManager(session)
                path = tax_manager.get_taxonomy_path(tax.id)
                taxonomies.append({
                    "taxonomy_id": tax.id,
                    "path": " > ".join(path),
                    "confidence": link.confidence
                })
            
            results.append({
                "id": str(node.id),
                "label": node.label,
                "semantic_label": node.semantic_label,
                "node_type": node.node_type,
                "taxonomies": taxonomies
            })
        
        return jsonify({
            "success": True,
            "nodes": results,
            "count": len(results)
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error searching nodes: {str(e)}"
        }), 500


@taxonomy_viewer_bp.route('/api/taxonomy/node/<node_id>/taxonomy/<int:taxonomy_id>', methods=['DELETE'])
def api_remove_node_taxonomy(node_id, taxonomy_id):
    """Remove a taxonomy classification from a node"""
    try:
        import uuid as uuid_module
        node_uuid = uuid_module.UUID(node_id)
    except ValueError:
        return jsonify({"success": False, "message": "Invalid node ID format"}), 400
    
    try:
        session = get_tracked_session()
        
        # Delete the specific taxonomy link
        deleted = session.query(NodeTaxonomyLink).filter(
            and_(
                NodeTaxonomyLink.node_id == node_uuid,
                NodeTaxonomyLink.taxonomy_id == taxonomy_id
            )
        ).delete()
        
        if deleted == 0:
            return jsonify({
                "success": False,
                "message": "Taxonomy classification not found"
            }), 404
        
        session.commit()
        
        return jsonify({
            "success": True,
            "message": "Taxonomy classification removed successfully"
        })
        
    except Exception as e:
        session.rollback()
        return jsonify({
            "success": False,
            "message": f"Error removing taxonomy: {str(e)}"
        }), 500


@taxonomy_viewer_bp.route('/api/taxonomy/node/<node_id>/taxonomy/<int:old_taxonomy_id>', methods=['PUT'])
def api_update_node_taxonomy(node_id, old_taxonomy_id):
    """Update a node's taxonomy classification"""
    import uuid as uuid_module
    import traceback
    
    try:
        node_uuid = uuid_module.UUID(node_id)
    except ValueError:
        return jsonify({"success": False, "message": "Invalid node ID format"}), 400
    
    data = request.get_json(silent=True) or {}
    new_path = data.get('new_path', '').strip()
    
    if not new_path:
        return jsonify({"success": False, "message": "new_path is required"}), 400
    
    try:
        session = get_tracked_session()
        tax_manager = TaxonomyManager(session)
        
        # Find or create taxonomy from path
        path_parts = [p.strip() for p in new_path.split('>')]
        
        # Find taxonomy ID from path
        current_taxonomy_id = None
        for part in path_parts:
            if current_taxonomy_id is None:
                # Root level
                taxonomy = session.query(Taxonomy).filter(
                    and_(Taxonomy.label == part, Taxonomy.parent_id.is_(None))
                ).first()
            else:
                # Child level
                taxonomy = session.query(Taxonomy).filter(
                    and_(Taxonomy.label == part, Taxonomy.parent_id == current_taxonomy_id)
                ).first()
            
            if not taxonomy:
                return jsonify({
                    "success": False,
                    "message": f"Taxonomy path not found: {new_path}"
                }), 404
            
            current_taxonomy_id = taxonomy.id
        
        # Update the taxonomy link
        link = session.query(NodeTaxonomyLink).filter(
            and_(
                NodeTaxonomyLink.node_id == node_uuid,
                NodeTaxonomyLink.taxonomy_id == old_taxonomy_id
            )
        ).first()
        
        if not link:
            return jsonify({
                "success": False,
                "message": "Original taxonomy classification not found"
            }), 404
        
        link.taxonomy_id = current_taxonomy_id
        session.commit()
        
        return jsonify({
            "success": True,
            "message": "Taxonomy updated successfully"
        })
        
    except Exception as e:
        session.rollback()
        traceback.print_exc()
        return jsonify({
            "success": False,
            "message": f"Error updating taxonomy: {str(e)}"
        }), 500

