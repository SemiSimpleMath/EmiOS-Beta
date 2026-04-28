"""
Node Processing Manager

Manages the node processing status database and provides utilities for tracking
node processing, scheduling, and learning from user interactions.
"""

from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, asc
from app.models.base import get_session
from app.assistant.utils.logging_config import get_logger

from ..data_models.node_processing_tracking import (
    NodeProcessingStatus, 
    NodeProcessingBatch, 
    NodeProcessingStatistics
)

logger = get_logger(__name__)

class NodeProcessingManager:
    """
    Manages node processing status and provides utilities for the pipeline.
    """
    
    def __init__(self):
        self.session = get_session()
        
    def create_node_status(self, node_id: str, problem_description: str = None, 
                          problem_category: str = None) -> NodeProcessingStatus:
        """
        Create a new node processing status record.
        
        Args:
            node_id: ID of the node being processed
            problem_description: Description of the identified problem
            problem_category: Category of the problem (missing_data, orphaned, structural, content, etc.)
            
        Returns:
            NodeProcessingStatus object
        """
        try:
            status = NodeProcessingStatus(
                node_id=node_id,
                problem_description=problem_description,
                problem_category=problem_category,
                current_stage='pending'
            )
            
            self.session.add(status)
            self.session.commit()
            
            logger.info(f"✅ Created processing status for node {node_id}")
            return status
            
        except Exception as e:
            logger.error(f"❌ Failed to create node status for {node_id}: {e}")
            self.session.rollback()
            raise
    
    def update_node_status(self, node_id: str, status: str, **kwargs) -> bool:
        """
        Update the status of a node.
        
        Args:
            node_id: ID of the node to update
            status: New status
            **kwargs: Additional fields to update
            
        Returns:
            True if successful, False otherwise
        """
        try:
            node_status = self.session.query(NodeProcessingStatus).filter(
                NodeProcessingStatus.node_id == node_id
            ).first()
            
            if not node_status:
                logger.warning(f"⚠️ No processing status found for node {node_id}")
                return False
                
            # Update status and timestamp
            node_status.status = status
            node_status.updated_at = datetime.now(timezone.utc)
            
            # Update additional fields
            for key, value in kwargs.items():
                if hasattr(node_status, key):
                    setattr(node_status, key, value)
                    
            # Set resolved_at if status is resolved
            if status == 'resolved':
                node_status.resolved_at = datetime.now(timezone.utc)
                
            self.session.commit()
            logger.info(f"✅ Updated node {node_id} status to {status}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to update node {node_id} status: {e}")
            self.session.rollback()
            return False
    
    def get_nodes_for_processing(self, max_nodes: int = 10, priority_order: bool = True) -> List[NodeProcessingStatus]:
        """
        Get nodes that need processing, prioritizing by status and priority.
        
        Args:
            max_nodes: Maximum number of nodes to return
            priority_order: Whether to order by priority
            
        Returns:
            List of NodeProcessingStatus objects
        """
        try:
            query = self.session.query(NodeProcessingStatus).filter(
                and_(
                    NodeProcessingStatus.status.in_(['pending', 'scheduled']),
                    NodeProcessingStatus.should_skip_future == False,
                    or_(
                        NodeProcessingStatus.next_review_at.is_(None),
                        NodeProcessingStatus.next_review_at <= datetime.now(timezone.utc)
                    )
                )
            )
            
            if priority_order:
                # Order by priority (critical > high > medium > low) then by creation time
                priority_order_map = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3}
                query = query.order_by(
                    asc(NodeProcessingStatus.priority),
                    asc(NodeProcessingStatus.first_identified_at)
                )
            else:
                query = query.order_by(asc(NodeProcessingStatus.first_identified_at))
                
            nodes = query.limit(max_nodes).all()
            
            logger.info(f"📋 Retrieved {len(nodes)} nodes for processing")
            return nodes
            
        except Exception as e:
            logger.error(f"❌ Failed to get nodes for processing: {e}")
            return []
    
    def get_scheduled_nodes(self, max_nodes: int = 10) -> List[NodeProcessingStatus]:
        """
        Get nodes that are scheduled for review.
        
        Args:
            max_nodes: Maximum number of nodes to return
            
        Returns:
            List of scheduled NodeProcessingStatus objects
        """
        try:
            nodes = self.session.query(NodeProcessingStatus).filter(
                and_(
                    NodeProcessingStatus.status == 'scheduled',
                    NodeProcessingStatus.next_review_at <= datetime.now(timezone.utc),
                    NodeProcessingStatus.should_skip_future == False
                )
            ).order_by(asc(NodeProcessingStatus.next_review_at)).limit(max_nodes).all()
            
            logger.info(f"📅 Retrieved {len(nodes)} scheduled nodes")
            return nodes
            
        except Exception as e:
            logger.error(f"❌ Failed to get scheduled nodes: {e}")
            return []
    
    def schedule_node_for_later(self, node_id: str, schedule_time: datetime = None, 
                               minutes_from_now: int = None) -> bool:
        """
        Schedule a node for later review.
        
        Args:
            node_id: ID of the node to schedule
            schedule_time: Specific time to schedule (if provided)
            minutes_from_now: Minutes from now to schedule (if provided)
            
        Returns:
            True if successful, False otherwise
        """
        try:
            if schedule_time is None and minutes_from_now is not None:
                schedule_time = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
            elif schedule_time is None:
                schedule_time = datetime.now(timezone.utc) + timedelta(hours=24)  # Default to tomorrow
                
            success = self.update_node_status(
                node_id=node_id,
                status='scheduled',
                next_review_at=schedule_time,
                last_offered_at=datetime.now(timezone.utc)
            )
            
            if success:
                logger.info(f"📅 Scheduled node {node_id} for {schedule_time}")
            return success
            
        except Exception as e:
            logger.error(f"❌ Failed to schedule node {node_id}: {e}")
            return False
    
    def mark_node_as_skipped(self, node_id: str, reason: str = None) -> bool:
        """
        Mark a node as skipped.
        
        Args:
            node_id: ID of the node to skip
            reason: Reason for skipping
            
        Returns:
            True if successful, False otherwise
        """
        try:
            success = self.update_node_status(
                node_id=node_id,
                status='skipped',
                notes=reason
            )
            
            if success:
                logger.info(f"⏭️ Marked node {node_id} as skipped: {reason}")
            return success
            
        except Exception as e:
            logger.error(f"❌ Failed to mark node {node_id} as skipped: {e}")
            return False
    
    def mark_node_as_invalid(self, node_id: str, reason: str = None) -> bool:
        """
        Mark a node as invalid (not a real problem).
        
        Args:
            node_id: ID of the node to mark as invalid
            reason: Reason for marking as invalid
            
        Returns:
            True if successful, False otherwise
        """
        try:
            success = self.update_node_status(
                node_id=node_id,
                status='invalid',
                user_marked_invalid=True,
                should_skip_future=True,  # Don't offer this again
                notes=reason
            )
            
            if success:
                logger.info(f"❌ Marked node {node_id} as invalid: {reason}")
            return success
            
        except Exception as e:
            logger.error(f"❌ Failed to mark node {node_id} as invalid: {e}")
            return False
    
    def record_user_response(self, node_id: str, user_response: str, response_type: str,
                           provided_data: Dict[str, Any] = None, confidence: float = None) -> bool:
        """
        Record a user's response to a node.
        
        Args:
            node_id: ID of the node
            user_response: Raw user response text
            response_type: Type of response (provide_data, skip, ask_later, etc.)
            provided_data: Structured data provided by user
            confidence: User's confidence in their response
            
        Returns:
            True if successful, False otherwise
        """
        try:
            success = self.update_node_status(
                node_id=node_id,
                status='questioned',
                user_response=user_response,
                user_response_type=response_type,
                user_provided_data=provided_data,
                user_confidence=confidence,
                last_offered_at=datetime.now(timezone.utc),
                attempt_count=NodeProcessingStatus.attempt_count + 1
            )
            
            if success:
                logger.info(f"💬 Recorded user response for node {node_id}: {response_type}")
            return success
            
        except Exception as e:
            logger.error(f"❌ Failed to record user response for node {node_id}: {e}")
            return False
    
    def get_processing_statistics(self, days: int = 30) -> Dict[str, Any]:
        """
        Get processing statistics for the last N days.
        
        Args:
            days: Number of days to look back
            
        Returns:
            Dictionary containing statistics
        """
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            
            # Get basic counts
            total_nodes = self.session.query(NodeProcessingStatus).filter(
                NodeProcessingStatus.first_identified_at >= cutoff_date
            ).count()
            
            resolved_nodes = self.session.query(NodeProcessingStatus).filter(
                and_(
                    NodeProcessingStatus.status == 'resolved',
                    NodeProcessingStatus.first_identified_at >= cutoff_date
                )
            ).count()
            
            skipped_nodes = self.session.query(NodeProcessingStatus).filter(
                and_(
                    NodeProcessingStatus.status == 'skipped',
                    NodeProcessingStatus.first_identified_at >= cutoff_date
                )
            ).count()
            
            invalid_nodes = self.session.query(NodeProcessingStatus).filter(
                and_(
                    NodeProcessingStatus.status == 'invalid',
                    NodeProcessingStatus.first_identified_at >= cutoff_date
                )
            ).count()
            
            # Calculate rates
            resolution_rate = resolved_nodes / total_nodes if total_nodes > 0 else 0
            false_positive_rate = invalid_nodes / total_nodes if total_nodes > 0 else 0
            
            stats = {
                'total_nodes': total_nodes,
                'resolved_nodes': resolved_nodes,
                'skipped_nodes': skipped_nodes,
                'invalid_nodes': invalid_nodes,
                'resolution_rate': resolution_rate,
                'false_positive_rate': false_positive_rate,
                'period_days': days
            }
            
            logger.info(f"📊 Retrieved processing statistics: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"❌ Failed to get processing statistics: {e}")
            return {}
    
    def get_nodes_to_skip(self) -> List[str]:
        """
        Get list of node IDs that should be skipped in future processing.
        
        Returns:
            List of node IDs to skip
        """
        try:
            nodes = self.session.query(NodeProcessingStatus.node_id).filter(
                NodeProcessingStatus.should_skip_future == True
            ).all()
            
            node_ids = [str(node_id[0]) for node_id in nodes]
            logger.info(f"⏭️ Retrieved {len(node_ids)} nodes to skip")
            return node_ids
            
        except Exception as e:
            logger.error(f"❌ Failed to get nodes to skip: {e}")
            return []
    
    def cleanup_old_records(self, days_to_keep: int = 90) -> int:
        """
        Clean up old processing records.
        
        Args:
            days_to_keep: Number of days of records to keep
            
        Returns:
            Number of records deleted
        """
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
            
            # Delete old resolved/skipped/invalid records
            deleted_count = self.session.query(NodeProcessingStatus).filter(
                and_(
                    NodeProcessingStatus.status.in_(['resolved', 'skipped', 'invalid']),
                    NodeProcessingStatus.resolved_at < cutoff_date
                )
            ).delete()
            
            self.session.commit()
            
            logger.info(f"🧹 Cleaned up {deleted_count} old processing records")
            return deleted_count
            
        except Exception as e:
            logger.error(f"❌ Failed to cleanup old records: {e}")
            self.session.rollback()
            return 0
