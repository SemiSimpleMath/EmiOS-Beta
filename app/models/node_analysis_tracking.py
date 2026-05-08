"""
Node Analysis Tracking System
Tracks which nodes have been analyzed by the cleanup pipeline
SQLite Compatible Version
"""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, DateTime, Integer, Float, Boolean, Text, Index, func
)
from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now

from app.models.base import Base, get_session

# Helper to generate string UUIDs for SQLite
def generate_uuid():
    return str(uuid.uuid4())


class NodeAnalysisTracking(Base):
    """
    Tracks which nodes have been analyzed by the node cleanup pipeline
    """
    __tablename__ = 'node_analysis_tracking'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Text, nullable=False)  # SQLite: UUID as TEXT
    node_label = Column(String(255), nullable=False)  # For easy querying
    analysis_timestamp = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    
    # Analysis results
    is_suspect = Column(Boolean, nullable=False)  # Whether the node was flagged as suspect
    suspect_reason = Column(Text, nullable=True)  # Reason if suspect
    confidence = Column(Float, nullable=True)  # Confidence score (0.0-1.0)
    cleanup_priority = Column(String(50), nullable=True)  # 'high', 'medium', 'low', 'none'
    suggested_action = Column(Text, nullable=True)  # 'delete', 'merge', 'keep', etc.
    
    # Node state at analysis time (for comparison)
    edge_count_at_analysis = Column(Integer, nullable=True)
    user_distance_at_analysis = Column(Integer, nullable=True)
    node_type_at_analysis = Column(String(100), nullable=True)
    node_classification_at_analysis = Column(String(100), nullable=True)
    
    # Processing metadata
    analysis_duration_seconds = Column(Float, nullable=True)  # How long analysis took
    agent_version = Column(String(100), nullable=True)  # Version of cleanup agent used
    
    # Timestamps
    created_at = Column(AwareUtcDateTime, default=utc_now)
    updated_at = Column(AwareUtcDateTime, default=utc_now, onupdate=utc_now)
    
    __table_args__ = (
        Index('ix_node_analysis_tracking_node_id', 'node_id'),
        Index('ix_node_analysis_tracking_analysis_timestamp', 'analysis_timestamp'),
        Index('ix_node_analysis_tracking_is_suspect', 'is_suspect'),
        Index('ix_node_analysis_tracking_cleanup_priority', 'cleanup_priority'),
        Index('ix_node_analysis_tracking_created_at', 'created_at'),
    )


# Database management functions
def initialize_node_analysis_tracking_db():
    """Initialize the node analysis tracking table"""
    session = get_session()
    engine = session.bind
    print(f"🔍 Node Analysis Tracking Debug: Connecting to database: {engine.url}")
    Base.metadata.create_all(engine, checkfirst=True)
    session.close()
    print("✅ node_analysis_tracking table initialized successfully.")


def get_node_analysis_status(session, node_id: str) -> dict:
    """
    Get the analysis status of a specific node
    
    Args:
        session: Database session
        node_id: UUID of the node to check
        
    Returns:
        Dictionary with analysis status, or None if not analyzed
    """
    tracking = session.query(NodeAnalysisTracking).filter(
        NodeAnalysisTracking.node_id == node_id
    ).first()
    
    if tracking:
        return {
            'node_id': str(tracking.node_id),
            'node_label': tracking.node_label,
            'analysis_timestamp': tracking.analysis_timestamp,
            'is_suspect': tracking.is_suspect,
            'suspect_reason': tracking.suspect_reason,
            'confidence': tracking.confidence,
            'cleanup_priority': tracking.cleanup_priority,
            'suggested_action': tracking.suggested_action,
            'edge_count_at_analysis': tracking.edge_count_at_analysis,
            'user_distance_at_analysis': tracking.user_distance_at_analysis,
            'node_type_at_analysis': tracking.node_type_at_analysis,
            'node_classification_at_analysis': tracking.node_classification_at_analysis,
            'analysis_duration_seconds': tracking.analysis_duration_seconds,
            'agent_version': tracking.agent_version,
            'created_at': tracking.created_at,
            'updated_at': tracking.updated_at
        }
    return None


def mark_node_as_analyzed(session, node_data: dict, analysis_result: dict, analysis_duration: float = None, agent_version: str = None):
    """
    Mark a node as analyzed and store the results
    
    Args:
        session: Database session
        node_data: Dictionary with node information
        analysis_result: Dictionary with analysis results from the agent
        analysis_duration: How long the analysis took in seconds
        agent_version: Version of the cleanup agent used
    """
    # Check if node was already analyzed
    existing = session.query(NodeAnalysisTracking).filter(
        NodeAnalysisTracking.node_id == node_data['node_id']
    ).first()
    
    if existing:
        # Update existing record
        existing.analysis_timestamp = datetime.now(timezone.utc)
        existing.is_suspect = analysis_result.get('suspect', False)
        existing.suspect_reason = analysis_result.get('suspect_reason')
        existing.confidence = analysis_result.get('confidence')
        existing.cleanup_priority = analysis_result.get('cleanup_priority')
        existing.suggested_action = analysis_result.get('suggested_action')
        existing.edge_count_at_analysis = node_data.get('edge_count')
        existing.user_distance_at_analysis = node_data.get('user_distance')
        existing.node_type_at_analysis = node_data.get('type')
        existing.node_classification_at_analysis = node_data.get('node_classification')
        existing.analysis_duration_seconds = analysis_duration
        existing.agent_version = agent_version
        existing.updated_at = datetime.now(timezone.utc)
    else:
        # Create new record
        tracking = NodeAnalysisTracking(
            node_id=node_data['node_id'],
            node_label=node_data['label'],
            analysis_timestamp=datetime.now(timezone.utc),
            is_suspect=analysis_result.get('suspect', False),
            suspect_reason=analysis_result.get('suspect_reason'),
            confidence=analysis_result.get('confidence'),
            cleanup_priority=analysis_result.get('cleanup_priority'),
            suggested_action=analysis_result.get('suggested_action'),
            edge_count_at_analysis=node_data.get('edge_count'),
            user_distance_at_analysis=node_data.get('user_distance'),
            node_type_at_analysis=node_data.get('type'),
            node_classification_at_analysis=node_data.get('node_classification'),
            analysis_duration_seconds=analysis_duration,
            agent_version=agent_version
        )
        session.add(tracking)
    
    session.commit()


def get_nodes_needing_analysis(session, limit: int = None) -> list:
    """
    Get nodes that haven't been analyzed yet
    
    Args:
        session: Database session
        limit: Maximum number of nodes to return (None for all)
        
    Returns:
        List of node IDs that need analysis
    """
    # Get all node IDs that have been analyzed
    analyzed_node_ids = session.query(NodeAnalysisTracking.node_id).all()
    analyzed_node_ids = [str(uid[0]) for uid in analyzed_node_ids]
    
    # Get nodes that haven't been analyzed
    from app.assistant.kg.db.knowledge_graph_db import Node
    
    query = session.query(Node.id).filter(
        ~Node.id.in_(analyzed_node_ids) if analyzed_node_ids else True
    )
    
    if limit:
        query = query.limit(limit)
    
    unanalyzed_nodes = query.all()
    return [str(node_id[0]) for node_id in unanalyzed_nodes]


def get_analysis_statistics(session) -> dict:
    """
    Get statistics about node analysis coverage
    
    Returns:
        Dictionary with analysis statistics
    """
    from app.assistant.kg.db.knowledge_graph_db import Node
    
    total_nodes = session.query(Node).count()
    analyzed_nodes = session.query(NodeAnalysisTracking).count()
    suspect_nodes = session.query(NodeAnalysisTracking).filter(
        NodeAnalysisTracking.is_suspect == True
    ).count()
    
    # Get priority breakdown
    priority_counts = {}
    for priority in ['high', 'medium', 'low', 'none']:
        count = session.query(NodeAnalysisTracking).filter(
            NodeAnalysisTracking.cleanup_priority == priority
        ).count()
        priority_counts[priority] = count
    
    # Get recent analysis activity
    from datetime import datetime, timezone, timedelta
    last_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    recent_analysis = session.query(NodeAnalysisTracking).filter(
        NodeAnalysisTracking.analysis_timestamp >= last_24h
    ).count()
    
    return {
        'total_nodes': total_nodes,
        'analyzed_nodes': analyzed_nodes,
        'unanalyzed_nodes': total_nodes - analyzed_nodes,
        'coverage_percentage': (analyzed_nodes / total_nodes * 100) if total_nodes > 0 else 0,
        'suspect_nodes': suspect_nodes,
        'priority_breakdown': priority_counts,
        'recent_analysis_24h': recent_analysis
    }


def cleanup_old_analysis_records(session, days_to_keep: int = 30):
    """
    Clean up old analysis records to keep the table manageable
    
    Args:
        session: Database session
        days_to_keep: Number of days of analysis history to keep
    """
    from datetime import datetime, timezone, timedelta
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep)
    
    deleted_count = session.query(NodeAnalysisTracking).filter(
        NodeAnalysisTracking.analysis_timestamp < cutoff_date
    ).delete()
    
    session.commit()
    return deleted_count
