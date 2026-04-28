#!/usr/bin/env python3
"""
Taxonomy Reviewer Web Interface
Flask web application for reviewing taxonomy classifications, approving suggestions, and viewing the taxonomy hierarchy.
"""
import os
import sys
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, render_template, request, jsonify
from sqlalchemy import and_, or_
from app.models.base import get_session
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.kg_core.taxonomy.models import (
    Taxonomy, NodeTaxonomyLink, TaxonomySuggestions,
    NodeTaxonomyReviewQueue
)
from app.assistant.kg_core.taxonomy.manager import TaxonomyManager

app = Flask(__name__)
app.secret_key = 'taxonomy_reviewer_secret_key_2024'

# Track sessions per request for proper cleanup
from flask import g

@app.before_request
def before_request():
    """Initialize session tracking for this request"""
    g.sessions = []

@app.teardown_appcontext
def shutdown_session(exception=None):
    """Close all sessions created during this request"""
    sessions = getattr(g, 'sessions', [])
    for session in sessions:
        try:
            session.close()
        except Exception as e:
            print(f"DEBUG: Failed to close session during teardown: {e}")

def get_tracked_session():
    """Get a session that will be automatically closed"""
    session = get_session()
    if hasattr(g, 'sessions'):
        g.sessions.append(session)
    return session


class TaxonomyReviewManager:
    """Manager for taxonomy review operations"""
    
    def __init__(self, session=None):
        self.session = session or get_session()
        self.tax_manager = TaxonomyManager(self.session)
    
    def get_taxonomy_tree(self) -> Dict[str, Any]:
        """Get the full taxonomy tree structure"""
        try:
            # Get all root nodes (no parent)
            root_nodes = self.session.query(Taxonomy).filter(
                Taxonomy.parent_id.is_(None)
            ).all()
            
            tree = []
            for root in root_nodes:
                tree.append(self._build_tree_node(root))
            
            return {
                "success": True,
                "tree": tree,
                "total_types": self.session.query(Taxonomy).count()
            }
        
        except Exception as e:
            return {
                "success": False,
                "message": f"Error loading taxonomy tree: {str(e)}"
            }
    
    def _build_tree_node(self, taxonomy_node: Taxonomy) -> Dict[str, Any]:
        """Recursively build tree structure for a taxonomy node"""
        # Get usage statistics for this taxonomy type
        usage_count = self.session.query(NodeTaxonomyLink).filter(
            NodeTaxonomyLink.taxonomy_id == taxonomy_node.id
        ).count()
        
        # Get children
        children = self.session.query(Taxonomy).filter(
            Taxonomy.parent_id == taxonomy_node.id
        ).order_by(Taxonomy.label).all()
        
        node_data = {
            "id": taxonomy_node.id,
            "label": taxonomy_node.label,
            "parent_id": taxonomy_node.parent_id,
            "description": taxonomy_node.description,
            "usage_count": usage_count,
            "children": [self._build_tree_node(child) for child in children]
        }
        
        return node_data
    
    def get_taxonomy_path(self, taxonomy_id: int) -> List[str]:
        """Get the full path from root to a taxonomy node"""
        return self.tax_manager.get_taxonomy_path(taxonomy_id)
    
    def cleanup_approved_suggestions(self) -> Dict[str, Any]:
        """
        Clean up pending suggestions by checking if their suggested taxonomy types already exist.
        If they exist, classify the example nodes and remove the suggestion.
        """
        try:
            # Get all pending suggestions
            pending_suggestions = self.session.query(TaxonomySuggestions).filter(
                TaxonomySuggestions.status == 'pending'
            ).all()
            
            processed_count = 0
            classified_nodes = []
            removed_suggestions = []
            
            for suggestion in pending_suggestions:
                # Check if the suggested taxonomy type already exists
                full_path = f"{suggestion.parent_path} > {suggestion.suggested_label}"
                
                try:
                    # Check if taxonomy path exists
                    existing_taxonomy = self.tax_manager.find_taxonomy_by_path(full_path)
                    
                    if existing_taxonomy:
                        print(f"DEBUG: Found existing taxonomy for suggestion {suggestion.id}: {full_path}")
                        
                        # Classify all example nodes to the existing taxonomy type
                        suggestion_classified_nodes = []
                        if suggestion.example_nodes:
                            for example in suggestion.example_nodes:
                                node_id = example.get('node_id')
                                if node_id:
                                    try:
                                        # Convert string node_id to appropriate type
                                        if isinstance(node_id, str):
                                            try:
                                                # Try to convert to int first (for integer IDs)
                                                node_id_converted = int(node_id)
                                            except ValueError:
                                                # If it's not an integer, it's likely a UUID string
                                                node_id_converted = uuid.UUID(node_id)
                                        else:
                                            node_id_converted = node_id
                                        
                                        # Import the assign_taxonomy function
                                        from app.assistant.kg_core.taxonomy.taxonomy_pipeline import assign_taxonomy
                                        
                                        # Classify the node to the existing taxonomy type
                                        assign_taxonomy(
                                            node_id=node_id_converted,
                                            taxonomy_id=existing_taxonomy.id,
                                            confidence=0.9,  # High confidence since taxonomy exists
                                            source='auto_cleanup',
                                            provisional=False,
                                            session=self.session,
                                            tax_manager=self.tax_manager
                                        )
                                        
                                        suggestion_classified_nodes.append(node_id)
                                        classified_nodes.append(node_id)
                                        print(f"DEBUG: Auto-classified node {node_id} to existing taxonomy {existing_taxonomy.id}")
                                        
                                    except Exception as e:
                                        print(f"DEBUG: Error auto-classifying node {node_id}: {e}")
                                        # Rollback and continue with next node
                                        try:
                                            self.session.rollback()
                                        except Exception as e2:
                                            print(f"DEBUG: Rollback failed after auto-classify error: {e2}")
                                        continue
                        
                        # Mark suggestion as approved and remove it
                        suggestion.status = 'approved'
                        suggestion.reviewed_at = datetime.utcnow()
                        removed_suggestions.append({
                            'id': suggestion.id,
                            'path': full_path,
                            'classified_nodes': suggestion_classified_nodes
                        })
                        processed_count += 1
                        
                except Exception as e:
                    print(f"DEBUG: Error checking taxonomy path {full_path}: {e}")
                    continue
            
            if processed_count > 0:
                self.session.commit()
                print(f"DEBUG: Cleaned up {processed_count} suggestions, classified {len(classified_nodes)} nodes")
            
            return {
                "success": True,
                "processed_suggestions": processed_count,
                "classified_nodes": len(classified_nodes),
                "removed_suggestions": removed_suggestions
            }
            
        except Exception as e:
            print(f"DEBUG: Error in cleanup_approved_suggestions: {e}")
            self.session.rollback()
            return {
                "success": False,
                "error": str(e)
            }

    def get_pending_suggestions(self, limit: int = 100) -> Dict[str, Any]:
        """Get pending taxonomy suggestions"""
        try:
            # First, clean up any suggestions that can be auto-processed
            cleanup_result = self.cleanup_approved_suggestions()
            if cleanup_result.get("processed_suggestions", 0) > 0:
                print(f"DEBUG: Auto-processed {cleanup_result['processed_suggestions']} suggestions")
            
            # Get total count first (before applying limit)
            total_pending_count = self.session.query(TaxonomySuggestions).filter(
                TaxonomySuggestions.status == 'pending'
            ).count()
            
            suggestions = self.session.query(TaxonomySuggestions).filter(
                TaxonomySuggestions.status == 'pending'
            ).order_by(
                TaxonomySuggestions.confidence.desc()
            ).limit(limit).all()
            
            suggestion_list = []
            for suggestion in suggestions:
                # Get example nodes
                example_nodes_data = []
                if suggestion.example_nodes:
                    for example in suggestion.example_nodes:
                        node_id = example.get('node_id')
                        if node_id:
                            try:
                                node_uuid = uuid.UUID(node_id) if isinstance(node_id, str) else node_id
                                node = self.session.query(Node).filter(Node.id == node_uuid).first()
                                if node:
                                    example_nodes_data.append({
                                        "node_id": str(node.id),
                                        "label": node.label,
                                        "node_type": node.node_type,
                                        "sentence": node.original_sentence or ""
                                    })
                            except Exception as e:
                                print(f"Error loading example node {node_id}: {e}")
                
                suggestion_list.append({
                    "id": suggestion.id,
                    "parent_path": suggestion.parent_path,
                    "suggested_label": suggestion.suggested_label,
                    "description": suggestion.description,
                    "reasoning": suggestion.reasoning,
                    "confidence": suggestion.confidence,
                    "example_nodes": example_nodes_data,
                    "created_at": suggestion.created_at.isoformat() if suggestion.created_at else None
                })
            
            return {
                "success": True,
                "suggestions": suggestion_list,
                "total_count": total_pending_count,
                "displayed_count": len(suggestion_list)
            }
        
        except Exception as e:
            return {
                "success": False,
                "message": f"Error loading suggestions: {str(e)}"
            }
    
    def get_pending_node_reviews(self, limit: int = 100) -> Dict[str, Any]:
        """Get pending node classification reviews"""
        try:
            reviews = self.session.query(NodeTaxonomyReviewQueue).filter(
                NodeTaxonomyReviewQueue.status == 'pending'
            ).order_by(
                NodeTaxonomyReviewQueue.confidence.asc()  # Lowest confidence first
            ).limit(limit).all()
            
            review_list = []
            for review in reviews:
                # Get node details
                node = self.session.query(Node).filter(Node.id == review.node_id).first()
                
                if node:
                    review_list.append({
                        "id": review.id,
                        "node_id": str(review.node_id),
                        "node_label": node.label,
                        "node_semantic_label": node.semantic_label or "",
                        "node_type": node.node_type,
                        "node_sentence": node.original_sentence or "",
                        "proposed_path": review.proposed_path,
                        "validated_path": review.validated_path,
                        "action": review.action,
                        "confidence": review.confidence,
                        "reasoning": review.reasoning,
                        "created_at": review.created_at.isoformat() if review.created_at else None
                    })
            
            return {
                "success": True,
                "reviews": review_list,
                "total_count": len(review_list)
            }
        
        except Exception as e:
            return {
                "success": False,
                "message": f"Error loading node reviews: {str(e)}"
            }
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get taxonomy and classification statistics"""
        try:
            total_types = self.session.query(Taxonomy).count()
            total_nodes = self.session.query(Node).count()
            
            classified_nodes = self.session.query(NodeTaxonomyLink).with_entities(
                NodeTaxonomyLink.node_id
            ).distinct().count()
            
            pending_suggestions = self.session.query(TaxonomySuggestions).filter(
                TaxonomySuggestions.status == 'pending'
            ).count()
            
            pending_reviews = self.session.query(NodeTaxonomyReviewQueue).filter(
                NodeTaxonomyReviewQueue.status == 'pending'
            ).count()
            
            provisional_classifications = self.session.query(NodeTaxonomyLink).filter(
                or_(
                    NodeTaxonomyLink.source == 'probationary',
                    NodeTaxonomyLink.source == 'placeholder',
                    NodeTaxonomyLink.confidence < 0.7
                )
            ).with_entities(NodeTaxonomyLink.node_id).distinct().count()
            
            return {
                "success": True,
                "stats": {
                    "total_types": total_types,
                    "total_nodes": total_nodes,
                    "classified_nodes": classified_nodes,
                    "unclassified_nodes": total_nodes - classified_nodes,
                    "pending_suggestions": pending_suggestions,
                    "pending_reviews": pending_reviews,
                    "provisional_classifications": provisional_classifications
                }
            }
        
        except Exception as e:
            return {
                "success": False,
                "message": f"Error loading statistics: {str(e)}"
            }
    
    def approve_suggestion(self, suggestion_id: int, new_label: Optional[str] = None, new_parent_path: Optional[str] = None) -> Dict[str, Any]:
        """Approve a taxonomy suggestion and create the new taxonomy type"""
        try:
            print(f"DEBUG: approve_suggestion called with suggestion_id={suggestion_id}, new_label={new_label}, new_parent_path={new_parent_path}")
            
            # Get the suggestion
            suggestion = self.session.query(TaxonomySuggestions).filter(
                TaxonomySuggestions.id == suggestion_id
            ).first()
            
            if not suggestion:
                print(f"DEBUG: Suggestion {suggestion_id} not found")
                return {"success": False, "message": "Suggestion not found"}
            
            print(f"DEBUG: Found suggestion: parent_path={suggestion.parent_path}, suggested_label={suggestion.suggested_label}")
            
            # Determine if user edited the path
            if new_parent_path:
                # User edited the path → use EXACTLY what they typed (no appending)
                full_path = new_parent_path
                print(f"DEBUG: Using user's edited path as-is: {full_path}")
            else:
                # User didn't edit → use original parent + label
                label_to_use = new_label if new_label else suggestion.suggested_label
                parent_path_to_use = suggestion.parent_path
                full_path = f"{parent_path_to_use} > {label_to_use}"
                print(f"DEBUG: Using original parent + label: {full_path}")
            
            # Use the safe taxonomy creation method
            print(f"DEBUG: Creating taxonomy path safely: {full_path}")
            result = self.tax_manager.create_taxonomy_path_safe(
                path=full_path,
                description=f"Created from suggestion: {suggestion.suggested_label}"
            )
            
            if not result["success"]:
                return {
                    "success": False,
                    "message": result["message"]
                }
            
            final_taxonomy_id = result["taxonomy_id"]
            print(f"DEBUG: Taxonomy path result: {result['message']}")
            
            # Mark suggestion as approved
            suggestion.status = 'approved'
            suggestion.reviewed_at = datetime.utcnow()
            
            # Classify all example nodes to the new taxonomy type
            classified_nodes = []
            if suggestion.example_nodes:
                print(f"DEBUG: Classifying {len(suggestion.example_nodes)} example nodes...")
                
                for example in suggestion.example_nodes:
                    node_id = example.get('node_id')
                    if node_id:
                        try:
                            # Convert string node_id to appropriate type
                            if isinstance(node_id, str):
                                try:
                                    # Try to convert to int first (for integer IDs)
                                    node_id_converted = int(node_id)
                                except ValueError:
                                    # If it's not an integer, it's likely a UUID string
                                    node_id_converted = uuid.UUID(node_id)
                            else:
                                node_id_converted = node_id
                            
                            # Import the assign_taxonomy function
                            from app.assistant.kg_core.taxonomy.taxonomy_pipeline import assign_taxonomy
                            
                            # Classify the node to the new taxonomy type
                            assign_taxonomy(
                                node_id=node_id_converted,
                                taxonomy_id=final_taxonomy_id,
                                confidence=0.9,  # High confidence since human approved
                                source='approved_suggestion',
                                provisional=False,  # Not provisional since human approved
                                session=self.session,
                                tax_manager=self.tax_manager
                            )
                            
                            classified_nodes.append(node_id)
                            print(f"DEBUG: Classified node {node_id} to taxonomy {final_taxonomy_id}")
                            
                        except Exception as e:
                            print(f"DEBUG: Error classifying node {node_id}: {e}")
                            # Rollback and continue with next node
                            try:
                                self.session.rollback()
                            except Exception as e2:
                                print(f"DEBUG: Rollback failed after classification error: {e2}")
                            continue
            
            print(f"DEBUG: Committing changes...")
            self.session.commit()
            
            # Get the full path for the created/found type
            new_path = self.tax_manager.get_taxonomy_path(final_taxonomy_id)
            
            print(f"DEBUG: Successfully created/found taxonomy: {' > '.join(new_path)}")
            
            return {
                "success": True,
                "message": f"Created taxonomy type: {' > '.join(new_path)} and classified {len(classified_nodes)} nodes",
                "new_taxonomy_id": final_taxonomy_id,
                "new_path": ' > '.join(new_path),
                "classified_nodes": classified_nodes
            }
        
        except Exception as e:
            import traceback
            print(f"DEBUG: Exception in approve_suggestion: {str(e)}")
            print(traceback.format_exc())
            self.session.rollback()
            return {
                "success": False,
                "message": f"Error approving suggestion: {str(e)}"
            }
    
    def reject_suggestion(self, suggestion_id: int) -> Dict[str, Any]:
        """Reject a taxonomy suggestion"""
        try:
            suggestion = self.session.query(TaxonomySuggestions).filter(
                TaxonomySuggestions.id == suggestion_id
            ).first()
            
            if not suggestion:
                return {"success": False, "message": "Suggestion not found"}
            
            suggestion.status = 'rejected'
            suggestion.reviewed_at = datetime.utcnow()
            
            self.session.commit()
            
            return {
                "success": True,
                "message": f"Rejected suggestion: {suggestion.suggested_label}"
            }
        
        except Exception as e:
            self.session.rollback()
            return {
                "success": False,
                "message": f"Error rejecting suggestion: {str(e)}"
            }
    
    def reject_node_review(self, review_id: int) -> Dict[str, Any]:
        """
        Truly reject a node taxonomy review - don't apply any taxonomy.
        Removes any existing taxonomy links and puts node back in unclassified queue.
        """
        try:
            review = self.session.query(NodeTaxonomyReviewQueue).filter(
                NodeTaxonomyReviewQueue.id == review_id
            ).first()
            
            if not review:
                return {"success": False, "message": "Review not found"}
            
            # Remove ALL taxonomy links for this node (put back in unclassified pool)
            deleted_count = self.session.query(NodeTaxonomyLink).filter(
                NodeTaxonomyLink.node_id == review.node_id
            ).delete()
            
            # Mark review as rejected (keep in database for auditing)
            review.status = 'rejected_both'
            review.reviewed_at = datetime.utcnow()
            
            self.session.commit()
            
            return {
                "success": True,
                "message": f"Rejected both proposer and critic. Removed {deleted_count} taxonomy link(s). Node '{review.node_label}' is back in unclassified queue."
            }
        
        except Exception as e:
            self.session.rollback()
            return {
                "success": False,
                "message": f"Error rejecting review: {str(e)}"
            }
    
    def accept_proposer_review(self, review_id: int) -> Dict[str, Any]:
        """
        Accept the proposer's original suggestion, bypassing the critic.
        This is rare but useful when the critic is wrong.
        """
        try:
            review = self.session.query(NodeTaxonomyReviewQueue).filter(
                NodeTaxonomyReviewQueue.id == review_id
            ).first()
            
            if not review:
                return {"success": False, "message": "Review not found"}
            
            if not review.proposed_path:
                return {"success": False, "message": "No proposer path available"}
            
            # Find or create the proposer's taxonomy path
            proposer_taxonomy_id = self.tax_manager.find_taxonomy_by_path(review.proposed_path)
            
            if not proposer_taxonomy_id:
                # Create the path if it doesn't exist
                proposer_taxonomy_id = self._create_taxonomy_path(review.proposed_path)
                
            if not proposer_taxonomy_id:
                return {
                    "success": False,
                    "message": f"Could not find or create taxonomy path: {review.proposed_path}"
                }
            
            # Remove ONLY the specific taxonomy link from the validated_path (critic's suggestion)
            # This is what we're rejecting in favor of the proposer's path
            validated_taxonomy_id = None
            if review.validated_path:
                validated_taxonomy_id = self.tax_manager.find_taxonomy_by_path(review.validated_path)
                
                if validated_taxonomy_id:
                    deleted_count = self.session.query(NodeTaxonomyLink).filter(
                        and_(
                            NodeTaxonomyLink.node_id == review.node_id,
                            NodeTaxonomyLink.taxonomy_id == validated_taxonomy_id
                        )
                    ).delete(synchronize_session='fetch')
                else:
                    deleted_count = 0
            else:
                deleted_count = 0
            
            # Apply the proposer's taxonomy (create or update)
            existing_link = self.session.query(NodeTaxonomyLink).filter(
                and_(
                    NodeTaxonomyLink.node_id == review.node_id,
                    NodeTaxonomyLink.taxonomy_id == proposer_taxonomy_id
                )
            ).first()
            
            if existing_link:
                # Link already exists, upgrade it
                existing_link.confidence = 0.85
                existing_link.source = 'manual'
                existing_link.last_seen = datetime.utcnow()
            else:
                # Create new link
                new_link = NodeTaxonomyLink(
                    node_id=review.node_id,
                    taxonomy_id=proposer_taxonomy_id,
                    confidence=0.85,  # High confidence - user explicitly chose this
                    source='manual',  # Manual override
                    count=1,
                    last_seen=datetime.utcnow()
                )
                self.session.add(new_link)
            
            # Mark review as proposer accepted
            review.status = 'proposer_accepted'
            review.reviewed_at = datetime.utcnow()
            
            self.session.commit()
            
            return {
                "success": True,
                "message": f"Accepted proposer's suggestion: {review.proposed_path} (bypassed critic)"
            }
        
        except Exception as e:
            self.session.rollback()
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "message": f"Error accepting proposer: {str(e)}"
            }
    
    def _create_taxonomy_path(self, path: str) -> Optional[int]:
        """
        Create a taxonomy path, creating any missing intermediate nodes.
        
        Args:
            path: Path string like "entity > person > software_developer"
            
        Returns:
            The taxonomy ID of the leaf node, or None if creation failed
        """
        try:
            # Split path into labels
            if " > " in path:
                labels = [label.strip() for label in path.split(" > ")]
            elif "." in path:
                labels = [label.strip() for label in path.split(".")]
            else:
                labels = [path.strip()]
            
            # Walk down the path, creating nodes as needed
            current_parent_id = None
            current_taxonomy = None
            
            for label in labels:
                # Try to find existing node
                if current_parent_id is None:
                    # Root level
                    existing = self.session.query(Taxonomy).filter(
                        Taxonomy.label == label,
                        Taxonomy.parent_id.is_(None)
                    ).first()
                else:
                    # Child level
                    existing = self.session.query(Taxonomy).filter(
                        Taxonomy.label == label,
                        Taxonomy.parent_id == current_parent_id
                    ).first()
                
                if existing:
                    current_taxonomy = existing
                    current_parent_id = existing.id
                else:
                    # Create new taxonomy node
                    new_taxonomy = Taxonomy(
                        label=label,
                        parent_id=current_parent_id,
                        description=f"Created via manual review"
                    )
                    self.session.add(new_taxonomy)
                    self.session.commit()  # Commit immediately - SQLite single-writer
                    current_taxonomy = new_taxonomy
                    current_parent_id = new_taxonomy.id
            
            return current_taxonomy.id if current_taxonomy else None
            
        except Exception as e:
            print(f"Error creating taxonomy path '{path}': {str(e)}")
            return None
    
    def approve_node_review(self, review_id: int, final_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Approve a node classification review.
        
        Process:
        1. Create the new taxonomy path if it doesn't exist
        2. Look up the OLD taxonomy_id from proposed_path (the wrong classification)
        3. Delete ONLY the specific (node_id, old_taxonomy_id) link
        4. Create the new (node_id, new_taxonomy_id) link
        5. Leave all other taxonomy links for this node untouched
        """
        try:
            review = self.session.query(NodeTaxonomyReviewQueue).filter(
                NodeTaxonomyReviewQueue.id == review_id
            ).first()
            
            if not review:
                return {"success": False, "message": "Review not found"}
            
            # Use edited path if provided, otherwise use validated path
            path_to_use = final_path if final_path else review.validated_path
            
            # STEP 1: Find or create the NEW taxonomy path
            new_taxonomy_id = self.tax_manager.find_taxonomy_by_path(path_to_use)
            
            if not new_taxonomy_id:
                # Path doesn't exist - create it
                new_taxonomy_id = self._create_taxonomy_path(path_to_use)
                if not new_taxonomy_id:
                    return {
                        "success": False,
                        "message": f"Failed to create taxonomy path: {path_to_use}"
                    }
            
            # STEP 2: Find the OLD taxonomy_id if there was a proposed path
            old_taxonomy_id = None
            if review.proposed_path:
                old_taxonomy_id = self.tax_manager.find_taxonomy_by_path(review.proposed_path)
            
            # STEP 3: Delete ONLY the specific old (node_id, old_taxonomy_id) link
            # This preserves any other taxonomy classifications this node might have
            if old_taxonomy_id:
                deleted_count = self.session.query(NodeTaxonomyLink).filter(
                    and_(
                        NodeTaxonomyLink.node_id == review.node_id,
                        NodeTaxonomyLink.taxonomy_id == old_taxonomy_id
                    )
                ).delete(synchronize_session='fetch')
                # Note: deleted_count will be 0 if the link doesn't exist (that's ok)
            
            # STEP 4: Create or update the new taxonomy link
            existing_link = self.session.query(NodeTaxonomyLink).filter(
                and_(
                    NodeTaxonomyLink.node_id == review.node_id,
                    NodeTaxonomyLink.taxonomy_id == new_taxonomy_id
                )
            ).first()
            
            if existing_link:
                # Link already exists, just update metadata
                existing_link.confidence = 0.95
                existing_link.source = 'manual'
                existing_link.last_seen = datetime.utcnow()
            else:
                # Create new link
                new_link = NodeTaxonomyLink(
                    node_id=review.node_id,
                    taxonomy_id=new_taxonomy_id,
                    confidence=0.95,
                    source='manual',
                    count=1,
                    last_seen=datetime.utcnow()
                )
                self.session.add(new_link)
            
            # Mark review as approved
            review.status = 'approved'
            review.reviewed_at = datetime.utcnow()
            review.final_taxonomy_path = path_to_use
            
            self.session.commit()
            
            return {
                "success": True,
                "message": f"Updated classification from '{review.proposed_path or 'none'}' to '{path_to_use}'"
            }
        
        except Exception as e:
            self.session.rollback()
            return {
                "success": False,
                "message": f"Error approving review: {str(e)}"
            }
    
    def reject_node_review(self, review_id: int) -> Dict[str, Any]:
        """
        Reject a node classification review.
        
        This removes ONLY the specific taxonomy link being reviewed,
        leaving all other taxonomy classifications intact.
        """
        try:
            review = self.session.query(NodeTaxonomyReviewQueue).filter(
                NodeTaxonomyReviewQueue.id == review_id
            ).first()
            
            if not review:
                return {"success": False, "message": "Review not found"}
            
            # Find the specific taxonomy_id being reviewed
            # Try validated_path first (the critic's suggestion), then proposed_path
            path_to_remove = review.validated_path or review.proposed_path
            
            if path_to_remove:
                taxonomy_id_to_remove = self.tax_manager.find_taxonomy_by_path(path_to_remove)
                
                if taxonomy_id_to_remove:
                    # Delete ONLY this specific taxonomy link
                    deleted_count = self.session.query(NodeTaxonomyLink).filter(
                        and_(
                            NodeTaxonomyLink.node_id == review.node_id,
                            NodeTaxonomyLink.taxonomy_id == taxonomy_id_to_remove
                        )
                    ).delete(synchronize_session='fetch')
                else:
                    deleted_count = 0
            else:
                deleted_count = 0
            
            # Mark review as rejected
            review.status = 'rejected'
            review.reviewed_at = datetime.utcnow()
            
            self.session.commit()
            
            return {
                "success": True,
                "message": f"Rejected classification '{path_to_remove}' (removed {deleted_count} link(s))"
            }
        
        except Exception as e:
            self.session.rollback()
            return {
                "success": False,
                "message": f"Error rejecting review: {str(e)}"
            }
    
    def edit_taxonomy_label(self, node_id: int, new_label: str) -> Dict[str, Any]:
        """Edit the label of a taxonomy node"""
        try:
            node = self.session.query(Taxonomy).filter(
                Taxonomy.id == node_id
            ).first()
            
            if not node:
                return {"success": False, "message": "Taxonomy node not found"}
            
            old_label = node.label
            node.label = new_label.strip().lower().replace(' ', '_')
            
            self.session.commit()
            
            return {
                "success": True,
                "message": f"Updated '{old_label}' to '{node.label}'"
            }
        
        except Exception as e:
            self.session.rollback()
            return {
                "success": False,
                "message": f"Error editing taxonomy: {str(e)}"
            }
    
    def edit_taxonomy(self, node_id: int, new_label: str, new_parent_id: Optional[int], new_description: Optional[str] = None) -> Dict[str, Any]:
        """Edit the label, parent, and description of a taxonomy node"""
        try:
            node = self.session.query(Taxonomy).filter(
                Taxonomy.id == node_id
            ).first()
            
            if not node:
                return {"success": False, "message": "Taxonomy node not found"}
            
            # Validate new parent (if provided)
            if new_parent_id is not None:
                new_parent = self.session.query(Taxonomy).filter(
                    Taxonomy.id == new_parent_id
                ).first()
                
                if not new_parent:
                    return {"success": False, "message": "New parent taxonomy node not found"}
                
                # Check for circular reference (new parent can't be a descendant)
                current = new_parent
                while current.parent_id:
                    if current.parent_id == node_id:
                        return {
                            "success": False,
                            "message": "Cannot move a node to be a child of its own descendant"
                        }
                    current = self.session.query(Taxonomy).filter(
                        Taxonomy.id == current.parent_id
                    ).first()
                    if not current:
                        break
            
            # Update node
            old_label = node.label
            old_parent_id = node.parent_id
            old_description = node.description
            
            node.label = new_label.strip().lower().replace(' ', '_')
            node.parent_id = new_parent_id
            
            # Update description if provided (including empty string to clear it)
            if new_description is not None:
                node.description = new_description.strip() if new_description.strip() else None
            
            self.session.commit()
            
            # Build message
            parts = []
            if old_label != node.label:
                parts.append(f"label from '{old_label}' to '{node.label}'")
            if old_parent_id != new_parent_id:
                if new_parent_id is None:
                    parts.append(f"moved to root level")
                else:
                    new_parent = self.session.query(Taxonomy).filter(
                        Taxonomy.id == new_parent_id
                    ).first()
                    parts.append(f"moved under '{new_parent.label}'")
            if new_description is not None and old_description != node.description:
                parts.append(f"description updated")
            
            message = f"Updated {' and '.join(parts)}" if parts else "No changes made"
            
            return {
                "success": True,
                "message": message
            }
        
        except Exception as e:
            self.session.rollback()
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "message": f"Error editing taxonomy: {str(e)}"
            }
    
    def delete_taxonomy(self, node_id: int) -> Dict[str, Any]:
        """Delete a taxonomy node and all its children (cascade)"""
        try:
            node = self.session.query(Taxonomy).filter(
                Taxonomy.id == node_id
            ).first()
            
            if not node:
                return {"success": False, "message": "Taxonomy node not found"}
            
            node_label = node.label
            
            # Count how many nodes will be deleted (including descendants)
            def count_descendants(parent_id):
                children = self.session.query(Taxonomy).filter(
                    Taxonomy.parent_id == parent_id
                ).all()
                count = len(children)
                for child in children:
                    count += count_descendants(child.id)
                return count
            
            total_to_delete = 1 + count_descendants(node_id)
            
            # Delete all NodeTaxonomyLinks that reference this node or its descendants
            def delete_links_recursive(tax_id):
                # Delete links for this taxonomy
                self.session.query(NodeTaxonomyLink).filter(
                    NodeTaxonomyLink.taxonomy_id == tax_id
                ).delete()
                
                # Recursively delete links for children
                children = self.session.query(Taxonomy).filter(
                    Taxonomy.parent_id == tax_id
                ).all()
                for child in children:
                    delete_links_recursive(child.id)
            
            delete_links_recursive(node_id)
            
            # Delete the taxonomy node (cascade will handle children)
            self.session.delete(node)
            self.session.commit()
            
            return {
                "success": True,
                "message": f"Deleted '{node_label}' and {total_to_delete - 1} descendant(s)"
            }
        
        except Exception as e:
            self.session.rollback()
            import traceback
            traceback.print_exc()
            return {
                "success": False,
                "message": f"Error deleting taxonomy: {str(e)}"
            }
    
    def add_taxonomy_child(self, parent_id: int, label: str) -> Dict[str, Any]:
        """Add a new child taxonomy node using safe creation"""
        try:
            parent = self.session.query(Taxonomy).filter(
                Taxonomy.id == parent_id
            ).first()
            
            if not parent:
                return {"success": False, "message": "Parent taxonomy node not found"}
            
            # Use the safe taxonomy creation method
            result = self.tax_manager.create_taxonomy_safe(
                label=label,
                parent_id=parent_id,
                description="Added via web interface",
                check_duplicates=True
            )
            
            if result["success"]:
                self.session.commit()
                return {
                    "success": True,
                    "message": f"Added '{result['taxonomy'].label}' as child of '{parent.label}'"
                }
            else:
                return {
                    "success": False,
                    "message": result["message"]
                }
        
        except Exception as e:
            self.session.rollback()
            return {
                "success": False,
                "message": f"Error adding taxonomy child: {str(e)}"
            }


# Helper function to get a fresh manager with a new session for each request
def get_review_manager():
    """Create a new TaxonomyReviewManager with a fresh session for this request."""
    return TaxonomyReviewManager(session=get_tracked_session())


# ============================================================================
# FLASK ROUTES
# ============================================================================

@app.route('/')
def index():
    """Main page"""
    return render_template('taxonomy_reviewer.html')


@app.route('/api/taxonomy/tree')
def api_taxonomy_tree():
    """Get the full taxonomy tree"""
    manager = get_review_manager()
    result = manager.get_taxonomy_tree()
    return jsonify(result)


@app.route('/api/taxonomy/statistics')
def api_statistics():
    """Get taxonomy and classification statistics"""
    manager = get_review_manager()
    result = manager.get_statistics()
    return jsonify(result)


@app.route('/api/taxonomy/suggestions')
def api_suggestions():
    """Get pending taxonomy suggestions"""
    limit = request.args.get('limit', 100, type=int)
    manager = get_review_manager()
    result = manager.get_pending_suggestions(limit=limit)
    return jsonify(result)


@app.route('/api/taxonomy/reviews')
def api_reviews():
    """Get pending node classification reviews"""
    limit = request.args.get('limit', 100, type=int)
    manager = get_review_manager()
    result = manager.get_pending_node_reviews(limit=limit)
    return jsonify(result)


@app.route('/api/taxonomy/suggestions/<int:suggestion_id>/approve', methods=['POST'])
def api_approve_suggestion(suggestion_id):
    """Approve a taxonomy suggestion"""
    try:
        # Use silent=True to avoid exception when body is empty
        data = request.get_json(silent=True) or {}
        new_label = data.get('new_label')  # Optional edited label
        new_parent_path = data.get('new_parent_path')  # Optional edited parent path
        
        manager = get_review_manager()
        result = manager.approve_suggestion(suggestion_id, new_label=new_label, new_parent_path=new_parent_path)
        return jsonify(result)
    except Exception as e:
        import traceback
        print(f"Error in api_approve_suggestion: {str(e)}")
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "message": f"Server error: {str(e)}"
        }), 500


@app.route('/api/taxonomy/suggestions/approve_all', methods=['POST'])
def api_approve_all_suggestions():
    """Approve all pending taxonomy suggestions"""
    try:
        manager = get_review_manager()
        
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
                "errors": errors[:5]  # Limit to first 5 errors
            })
            
    except Exception as e:
        import traceback
        print(f"Error in api_approve_all_suggestions: {str(e)}")
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "message": f"Server error: {str(e)}"
        }), 500


@app.route('/api/taxonomy/suggestions/<int:suggestion_id>/reject', methods=['POST'])
def api_reject_suggestion(suggestion_id):
    """Reject a taxonomy suggestion"""
    manager = get_review_manager()
    result = manager.reject_suggestion(suggestion_id)
    return jsonify(result)


@app.route('/api/taxonomy/reviews/<int:review_id>/approve', methods=['POST'])
def api_approve_review(review_id):
    """Approve a node classification review"""
    try:
        # Use silent=True to avoid exception when body is empty
        data = request.get_json(silent=True) or {}
        final_path = data.get('final_path')  # Optional edited path
        
        manager = get_review_manager()
        result = manager.approve_node_review(review_id, final_path=final_path)
        return jsonify(result)
    except Exception as e:
        import traceback
        print(f"Error in api_approve_review: {str(e)}")
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "message": f"Server error: {str(e)}"
        }), 500


@app.route('/api/taxonomy/reviews/<int:review_id>/reject', methods=['POST'])
def api_reject_review(review_id):
    """Reject both proposer and critic - put node back in unclassified queue"""
    manager = get_review_manager()
    result = manager.reject_node_review(review_id)
    return jsonify(result)


@app.route('/api/taxonomy/reviews/<int:review_id>/accept-proposer', methods=['POST'])
def api_accept_proposer(review_id):
    """Accept proposer's original suggestion, bypassing critic"""
    manager = get_review_manager()
    result = manager.accept_proposer_review(review_id)
    return jsonify(result)


@app.route('/api/taxonomy/cleanup-suggestions', methods=['POST'])
def api_cleanup_suggestions():
    """Clean up pending suggestions by auto-processing those with existing taxonomy types"""
    try:
        manager = get_review_manager()
        result = manager.cleanup_approved_suggestions()
        return jsonify(result)
    except Exception as e:
        import traceback
        print(f"Error in api_cleanup_suggestions: {str(e)}")
        print(traceback.format_exc())
        return jsonify({
            "success": False,
            "message": f"Server error: {str(e)}"
        }), 500


@app.route('/api/taxonomy/edit/<int:node_id>', methods=['POST'])
def api_edit_taxonomy(node_id):
    """Edit a taxonomy category label, parent, and/or description"""
    data = request.get_json() or {}
    new_label = data.get('new_label')
    new_parent_id = data.get('new_parent_id')  # Optional: change parent
    new_description = data.get('new_description')  # Optional: change description
    
    if not new_label:
        return jsonify({
            'success': False,
            'message': 'new_label is required'
        })
    
    manager = get_review_manager()
    
    # Get current node to preserve unchanged fields
    session = get_tracked_session()
    node = session.query(Taxonomy).filter(Taxonomy.id == node_id).first()
    if not node:
        return jsonify({'success': False, 'message': 'Taxonomy node not found'})
    
    # Determine parent_id to use
    if 'new_parent_id' in data:
        # Parent is being changed
        parent_id_to_use = None if new_parent_id == '' else new_parent_id
    else:
        # Parent not being changed, use current
        parent_id_to_use = node.parent_id
    
    # Always use edit_taxonomy since it handles all fields
    result = manager.edit_taxonomy(
        node_id, 
        new_label, 
        parent_id_to_use,
        new_description if 'new_description' in data else None
    )
    
    return jsonify(result)


@app.route('/api/taxonomy/delete/<int:node_id>', methods=['DELETE'])
def api_delete_taxonomy(node_id):
    """Delete a taxonomy category and all its children"""
    manager = get_review_manager()
    result = manager.delete_taxonomy(node_id)
    return jsonify(result)


@app.route('/api/taxonomy/export')
def api_export_taxonomy():
    """Export the entire taxonomy as JSON"""
    from app.assistant.kg_core.taxonomy.export import TaxonomyExporter
    
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


@app.route('/api/taxonomy/add', methods=['POST'])
def api_add_taxonomy():
    """Add a new taxonomy category as a child of an existing one"""
    data = request.get_json() or {}
    parent_id = data.get('parent_id')
    label = data.get('label')
    
    if not parent_id or not label:
        return jsonify({
            'success': False,
            'message': 'parent_id and label are required'
        })
    
    manager = get_review_manager()
    result = manager.add_taxonomy_child(parent_id, label)
    return jsonify(result)


@app.route('/api/taxonomy/search-nodes')
def api_search_nodes():
    """Search for nodes by label or UUID"""
    import uuid
    query = request.args.get('query', '').strip()
    
    if not query:
        return jsonify({"success": False, "message": "Query parameter required"}), 400
    
    try:
        session = get_tracked_session()
        
        # Try to parse as UUID first
        try:
            node_uuid = uuid.UUID(query)
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


@app.route('/api/taxonomy/node/<node_id>/taxonomy/<int:taxonomy_id>', methods=['DELETE'])
def api_remove_node_taxonomy(node_id, taxonomy_id):
    """Remove a taxonomy classification from a node"""
    try:
        import uuid
        node_uuid = uuid.UUID(node_id)
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


@app.route('/api/taxonomy/node/<node_id>/taxonomy/<int:old_taxonomy_id>', methods=['PUT'])
def api_update_node_taxonomy(node_id, old_taxonomy_id):
    """Update a node's taxonomy classification"""
    import uuid
    import traceback
    
    try:
        node_uuid = uuid.UUID(node_id)
    except ValueError:
        return jsonify({"success": False, "message": "Invalid node ID format"}), 400
    
    data = request.get_json(silent=True) or {}
    new_path = data.get('new_path', '').strip()
    
    if not new_path:
        return jsonify({"success": False, "message": "new_path is required"}), 400
    
    try:
        session = get_tracked_session()
        tax_manager = TaxonomyManager(session)
        
        # Find or create the taxonomy path
        new_taxonomy_id = tax_manager.find_taxonomy_by_path(new_path)
        
        if not new_taxonomy_id:
            # Create the path hierarchically
            path_parts = [p.strip() for p in new_path.split('>')]
            parent_id = None
            for part in path_parts:
                # Check if this level exists under the parent
                query = session.query(Taxonomy).filter(Taxonomy.label == part)
                if parent_id is not None:
                    query = query.filter(Taxonomy.parent_id == parent_id)
                else:
                    query = query.filter(Taxonomy.parent_id.is_(None))
                
                existing = query.first()
                if existing:
                    parent_id = existing.id
                else:
                    # Create new taxonomy node
                    new_tax = Taxonomy(label=part, parent_id=parent_id)
                    session.add(new_tax)
                    session.commit()  # Commit immediately - SQLite single-writer
                    parent_id = new_tax.id
            
            new_taxonomy_id = parent_id
            if not new_taxonomy_id:
                return jsonify({
                    "success": False,
                    "message": f"Could not create taxonomy path: {new_path}"
                }), 500
        
        # Check if the node already has this taxonomy
        existing_link = session.query(NodeTaxonomyLink).filter(
            and_(
                NodeTaxonomyLink.node_id == node_uuid,
                NodeTaxonomyLink.taxonomy_id == new_taxonomy_id
            )
        ).first()
        
        if existing_link:
            # Just remove the old one if it's different
            if old_taxonomy_id != new_taxonomy_id:
                session.query(NodeTaxonomyLink).filter(
                    and_(
                        NodeTaxonomyLink.node_id == node_uuid,
                        NodeTaxonomyLink.taxonomy_id == old_taxonomy_id
                    )
                ).delete()
                session.commit()
                return jsonify({
                    "success": True,
                    "message": "Taxonomy updated (already had the new classification)"
                })
            else:
                return jsonify({
                    "success": True,
                    "message": "No change needed"
                })
        
        # Delete the old taxonomy link
        deleted = session.query(NodeTaxonomyLink).filter(
            and_(
                NodeTaxonomyLink.node_id == node_uuid,
                NodeTaxonomyLink.taxonomy_id == old_taxonomy_id
            )
        ).delete()
        
        if deleted == 0:
            return jsonify({
                "success": False,
                "message": "Old taxonomy classification not found"
            }), 404
        
        # Create the new taxonomy link
        new_link = NodeTaxonomyLink(
            node_id=node_uuid,
            taxonomy_id=new_taxonomy_id,
            confidence=1.0,
            count=1
        )
        session.add(new_link)
        session.commit()
        
        return jsonify({
            "success": True,
            "message": f"Taxonomy updated to: {new_path}"
        })
        
    except Exception as e:
        session.rollback()
        traceback.print_exc()
        return jsonify({
            "success": False,
            "message": f"Error updating taxonomy: {str(e)}"
        }), 500


if __name__ == '__main__':
    print("🌐 Starting Taxonomy Reviewer Web Interface...")
    print("📱 Open your browser and go to: http://localhost:5002")
    print("\n✨ Features:")
    print("   • View and manage taxonomy hierarchy")
    print("   • Edit taxonomy category names")
    print("   • Add new child categories")
    print("   • Review and approve new taxonomy suggestions")
    print("   • Review and approve node classifications")
    print("   • Edit suggestions before approving")
    print("\n🚀 Loading...")
    app.run(debug=True, port=5002, host='0.0.0.0')

