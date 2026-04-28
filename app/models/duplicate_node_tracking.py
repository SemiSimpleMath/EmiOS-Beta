"""
Duplicate Node Tracking Model
Tracks duplicate node analysis results from the duplicate detection pipeline
SQLite Compatible Version
"""

import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, JSON, DateTime, Text, Integer, Boolean,
    UniqueConstraint, Index, func
)

from app.models.base import Base

# Helper to generate string UUIDs for SQLite
def generate_uuid():
    return str(uuid.uuid4())


class DuplicateNodeTracking(Base):
    """
    Tracks duplicate node analysis results from the duplicate detection pipeline
    """
    __tablename__ = 'duplicate_node_tracking'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    node_id = Column(Text, nullable=False)  # SQLite: UUID as TEXT
    node_label = Column(String(255), nullable=False)  # For easy querying
    node_type = Column(String(100), nullable=False)  # Node type for filtering
    
    # Analysis results
    is_duplicate = Column(Boolean, nullable=False, default=False)
    duplicate_confidence = Column(String(50), nullable=True)  # high, medium, low
    duplicate_reason = Column(Text, nullable=True)  # Why it's considered a duplicate
    similar_nodes = Column(JSON, nullable=True)  # SQLite: List of similar node IDs and details
    
    # Analysis metadata
    analysis_timestamp = Column(DateTime(timezone=True), nullable=False, default=func.now())
    batch_number = Column(Integer, nullable=True)  # Which batch this analysis came from
    agent_version = Column(String(50), nullable=True)  # Version of the duplicate detector agent
    
    # Additional context
    node_context = Column(Text, nullable=True)  # Context window or sentence for reference
    analysis_notes = Column(Text, nullable=True)  # Additional analysis notes
    
    # Indexes for performance
    __table_args__ = (
        Index('ix_duplicate_node_tracking_node_id', 'node_id'),
        Index('ix_duplicate_node_tracking_node_label', 'node_label'),
        Index('ix_duplicate_node_tracking_node_type', 'node_type'),
        Index('ix_duplicate_node_tracking_is_duplicate', 'is_duplicate'),
        Index('ix_duplicate_node_tracking_analysis_timestamp', 'analysis_timestamp'),
        Index('ix_duplicate_node_tracking_batch_number', 'batch_number'),
        UniqueConstraint('node_id', name='uq_duplicate_node_tracking_node_id'),
    )


def initialize_duplicate_node_tracking_db():
    """
    Initialize the duplicate_node_tracking table in the database
    """
    from app.models.base import get_default_engine
    from app.assistant.utils.logging_config import get_logger
    
    logger = get_logger(__name__)
    
    try:
        engine = get_default_engine()
        logger.info(f"🔧 Initializing duplicate_node_tracking table on engine: {engine.url}")
        
        # Create the table
        DuplicateNodeTracking.metadata.create_all(engine)
        logger.info("✅ duplicate_node_tracking table initialized successfully")
        
    except Exception as e:
        logger.error(f"❌ Failed to initialize duplicate_node_tracking table: {e}")
        raise


def clear_duplicate_node_tracking_data():
    """
    Clear all data from the duplicate_node_tracking table
    """
    from app.models.base import get_session
    from app.assistant.utils.logging_config import get_logger
    
    logger = get_logger(__name__)
    
    session = get_session()
    try:
        # Clear all records
        session.query(DuplicateNodeTracking).delete()
        session.commit()
        logger.info("✅ Cleared all duplicate_node_tracking data")
        
    except Exception as e:
        logger.error(f"❌ Failed to clear duplicate_node_tracking data: {e}")
        session.rollback()
        raise
    finally:
        session.close()
