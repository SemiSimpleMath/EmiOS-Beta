"""
KG Review Manager

Business logic for managing KG reviews: create, retrieve, update, and execute.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import uuid

from app.models.base import get_session
from app.assistant.kg_review.data_models.kg_review import (
    KGReview, 
    ReviewSource, 
    ReviewStatus, 
    ReviewPriority,
    FindingType
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class KGReviewManager:
    """Manages KG reviews: create, retrieve, update, and execute implementations."""
    
    def __init__(self):
        self.session = get_session()
    
    def create_review(
        self,
        node_id: str,
        problem_description: str,
        source: str,
        finding_type: Optional[str] = None,
        node_label: Optional[str] = None,
        node_type: Optional[str] = None,
        node_category: Optional[str] = None,
        analyzer_suggestion: Optional[str] = None,
        critic_suggestion: Optional[str] = None,
        priority: str = 'medium',
        confidence_score: Optional[float] = None,
        edge_count: Optional[int] = None,
        context_data: Optional[Dict] = None,
        source_pipeline_id: Optional[str] = None,
        **kwargs
    ) -> KGReview:
        """
        Create a new KG review.
        
        Args:
            node_id: UUID of the node
            problem_description: Description of the problem
            source: Source of the review (repair_pipeline, explorer, etc.)
            finding_type: Type of finding
            node_label: Label of the node (cached)
            node_type: Type of the node (cached)
            node_category: Category of the node (cached)
            analyzer_suggestion: Suggestion from analyzer
            critic_suggestion: Suggestion from critic
            priority: Priority level
            confidence_score: Confidence in the finding
            edge_count: Number of edges on the node
            context_data: Additional context
            source_pipeline_id: ID of the pipeline that created this
            **kwargs: Additional fields
            
        Returns:
            Created KGReview object
        """
        try:
            # Check if review already exists for this node with same problem
            # SQLite: use string for UUID comparison
            existing = self.session.query(KGReview).filter(
                KGReview.node_id == str(node_id),
                KGReview.problem_description == problem_description,
                KGReview.status.in_(['pending', 'under_review', 'approved'])
            ).first()
            
            if existing:
                logger.info(f"Review already exists for node {node_id} with same problem - updating instead")
                return self.update_review(
                    review_id=str(existing.id),
                    analyzer_suggestion=analyzer_suggestion or existing.analyzer_suggestion,
                    critic_suggestion=critic_suggestion or existing.critic_suggestion,
                    confidence_score=confidence_score or existing.confidence_score,
                    context_data=context_data or existing.context_data
                )
            
            review = KGReview(
                node_id=str(node_id),  # SQLite: store as string
                problem_description=problem_description,
                source=source,
                finding_type=finding_type,
                node_label=node_label,
                node_type=node_type,
                node_category=node_category,
                analyzer_suggestion=analyzer_suggestion,
                critic_suggestion=critic_suggestion,
                priority=priority,
                status='pending',
                confidence_score=confidence_score,
                edge_count=edge_count,
                context_data=context_data,
                source_pipeline_id=source_pipeline_id,
                **{k: v for k, v in kwargs.items() if hasattr(KGReview, k)}
            )
            
            self.session.add(review)
            self.session.commit()
            
            logger.info(f"✅ Created review {review.id} for node {node_label} ({node_id})")
            return review
            
        except Exception as e:
            self.session.rollback()
            logger.error(f"❌ Failed to create review: {e}")
            raise
    
    def get_reviews(
        self,
        status: Optional[str] = None,
        source: Optional[str] = None,
        priority: Optional[str] = None,
        node_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[KGReview]:
        """
        Retrieve reviews with optional filtering.
        
        Args:
            status: Filter by status
            source: Filter by source
            priority: Filter by priority
            node_id: Filter by specific node
            limit: Maximum number of reviews to return
            offset: Offset for pagination
            
        Returns:
            List of KGReview objects
        """
        try:
            query = self.session.query(KGReview)
            
            if status:
                query = query.filter(KGReview.status == status)
            if source:
                query = query.filter(KGReview.source == source)
            if priority:
                query = query.filter(KGReview.priority == priority)
            if node_id:
                # SQLite: use string for UUID comparison
                query = query.filter(KGReview.node_id == str(node_id))
            
            # Order by priority (high first) then by created date (newest first)
            priority_order = {
                'high': 1,
                'medium': 2,
                'low': 3,
                'none': 4
            }
            
            reviews = query.order_by(
                KGReview.created_at.desc()
            ).limit(limit).offset(offset).all()
            
            # Sort in Python by priority
            reviews.sort(key=lambda r: (
                priority_order.get(r.priority, 5),
                -r.created_at.timestamp() if r.created_at else 0
            ))
            
            return reviews
            
        except Exception as e:
            logger.error(f"❌ Failed to get reviews: {e}")
            return []
    
    def get_pending_reviews(self, limit: int = 100) -> List[KGReview]:
        """Get all pending reviews."""
        return self.get_reviews(status='pending', limit=limit)
    
    def get_approved_reviews(self, limit: int = 100) -> List[KGReview]:
        """Get all approved reviews ready for implementation."""
        return self.get_reviews(status='approved', limit=limit)
    
    def get_review(self, review_id: str) -> Optional[KGReview]:
        """Get a single review by ID."""
        try:
            # SQLite: use string for UUID comparison
            return self.session.query(KGReview).filter(
                KGReview.id == str(review_id)
            ).first()
        except Exception as e:
            logger.error(f"❌ Failed to get review {review_id}: {e}")
            return None
    
    def update_review(
        self,
        review_id: str,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        user_notes: Optional[str] = None,
        user_instructions: Optional[str] = None,
        reviewed_by: Optional[str] = None,
        is_false_positive: Optional[bool] = None,
        **kwargs
    ) -> Optional[KGReview]:
        """
        Update an existing review.
        
        Args:
            review_id: ID of the review to update
            status: New status
            priority: New priority
            user_notes: User notes
            user_instructions: Instructions for kg_team
            reviewed_by: Who reviewed it
            is_false_positive: Mark as false positive
            **kwargs: Additional fields to update
            
        Returns:
            Updated KGReview object or None if not found
        """
        try:
            review = self.get_review(review_id)
            if not review:
                logger.warning(f"Review {review_id} not found")
                return None
            
            if status is not None:
                review.status = status
                if status in ['approved', 'rejected']:
                    review.reviewed_at = datetime.now(timezone.utc)
            
            if priority is not None:
                review.priority = priority
            
            if user_notes is not None:
                review.user_notes = user_notes
            
            if user_instructions is not None:
                review.user_instructions = user_instructions
            
            if reviewed_by is not None:
                review.reviewed_by = reviewed_by
                review.reviewed_at = datetime.now(timezone.utc)
            
            if is_false_positive is not None:
                review.is_false_positive = is_false_positive
                if is_false_positive:
                    review.status = 'rejected'
            
            # Update any additional fields
            for key, value in kwargs.items():
                if hasattr(review, key):
                    setattr(review, key, value)
            
            review.updated_at = datetime.now(timezone.utc)
            self.session.commit()
            
            logger.info(f"✅ Updated review {review_id}")
            return review
            
        except Exception as e:
            self.session.rollback()
            logger.error(f"❌ Failed to update review {review_id}: {e}")
            return None
    
    def execute_review(self, review_id: str) -> Dict[str, Any]:
        """
        Execute a single approved review by delegating to kg_team.
        
        Args:
            review_id: ID of the review to execute
            
        Returns:
            Dict with execution results
        """
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message
            from app.assistant.kg_repair_pipeline.utils.kg_operations import KGOperations
            from app.assistant.kg_repair_pipeline.data_models.problematic_node import ProblematicNode
            from app.assistant.kg_repair_pipeline.stages.implementer import KGImplementer
            
            review = self.get_review(review_id)
            if not review:
                return {'success': False, 'error': f'Review {review_id} not found'}
            
            if review.status != 'approved':
                return {'success': False, 'error': f'Review must be approved before execution (current status: {review.status})'}
            
            if not review.user_instructions and not review.critic_suggestion:
                return {'success': False, 'error': 'No implementation instructions available'}
            
            logger.info(f"🔧 Executing review {review_id} for node {review.node_label}")
            
            # Mark as implementing
            self.update_review(review_id, status='implementing', implementation_started_at=datetime.now(timezone.utc))
            
            # Get node info from KG
            kg_ops = KGOperations()
            node_info = kg_ops.get_node_info(str(review.node_id))
            
            if not node_info:
                self.update_review(
                    review_id,
                    status='failed',
                    implementation_error='Node not found in knowledge graph'
                )
                return {'success': False, 'error': 'Node not found in knowledge graph'}
            
            # Create problematic node object
            problematic_node = ProblematicNode(
                id=str(review.node_id),
                label=node_info['label'],
                type=node_info['node_type'],
                category=node_info.get('category', ''),
                description=node_info['description'],
                start_date=node_info['start_date'],
                end_date=node_info['end_date'],
                start_date_confidence=node_info['start_date_confidence'],
                end_date_confidence=node_info['end_date_confidence'],
                valid_during=node_info['valid_during'],
                node_aliases=node_info['aliases'],
                full_node_info=node_info,
                problem_description=review.problem_description
            )
            
            # Use user instructions or fall back to critic suggestion
            instructions = review.user_instructions or review.critic_suggestion
            
            # Create user response object for implementer
            user_response_obj = type('UserResponse', (), {
                'node_id': str(review.node_id),
                'raw_response': instructions,
                'provided_data': {'instructions': instructions}
            })()
            
            # Execute via implementer (which delegates to kg_team)
            implementer = KGImplementer()
            result = implementer.implement_with_multi_tools(problematic_node, user_response_obj)
            
            # Update review with results
            if result['success']:
                self.update_review(
                    review_id,
                    status='completed',
                    implementation_completed_at=datetime.now(timezone.utc),
                    implementation_result=result.get('notes', 'Successfully implemented')
                )
                logger.info(f"✅ Successfully executed review {review_id}")
            else:
                self.update_review(
                    review_id,
                    status='failed',
                    implementation_error=result.get('error', 'Unknown error')
                )
                logger.error(f"❌ Failed to execute review {review_id}: {result.get('error')}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Error executing review {review_id}: {e}")
            self.update_review(
                review_id,
                status='failed',
                implementation_error=str(e)
            )
            return {'success': False, 'error': str(e)}
    
    def execute_batch(self, review_ids: List[str]) -> Dict[str, Any]:
        """
        Execute multiple approved reviews in batch.
        
        Args:
            review_ids: List of review IDs to execute
            
        Returns:
            Dict with batch execution results
        """
        try:
            logger.info(f"🚀 Executing batch of {len(review_ids)} reviews")
            
            results = {
                'total': len(review_ids),
                'succeeded': 0,
                'failed': 0,
                'details': []
            }
            
            for review_id in review_ids:
                result = self.execute_review(review_id)
                
                if result['success']:
                    results['succeeded'] += 1
                else:
                    results['failed'] += 1
                
                results['details'].append({
                    'review_id': review_id,
                    'success': result['success'],
                    'message': result.get('notes') or result.get('error', 'Unknown')
                })
            
            logger.info(f"✅ Batch execution complete: {results['succeeded']} succeeded, {results['failed']} failed")
            return results
            
        except Exception as e:
            logger.error(f"❌ Batch execution failed: {e}")
            return {
                'total': len(review_ids),
                'succeeded': 0,
                'failed': len(review_ids),
                'error': str(e)
            }
    
    def get_stats(self) -> Dict[str, Any]:
        """Get statistics about reviews."""
        try:
            from sqlalchemy import func
            
            total = self.session.query(func.count(KGReview.id)).scalar()
            
            by_status = {}
            status_counts = self.session.query(
                KGReview.status,
                func.count(KGReview.id)
            ).group_by(KGReview.status).all()
            
            for status, count in status_counts:
                by_status[status] = count
            
            by_source = {}
            source_counts = self.session.query(
                KGReview.source,
                func.count(KGReview.id)
            ).group_by(KGReview.source).all()
            
            for source, count in source_counts:
                by_source[source] = count
            
            by_priority = {}
            priority_counts = self.session.query(
                KGReview.priority,
                func.count(KGReview.id)
            ).group_by(KGReview.priority).all()
            
            for priority, count in priority_counts:
                by_priority[priority] = count
            
            return {
                'total': total,
                'by_status': by_status,
                'by_source': by_source,
                'by_priority': by_priority
            }
            
        except Exception as e:
            logger.error(f"❌ Failed to get stats: {e}")
            return {}

