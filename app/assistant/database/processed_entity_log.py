"""
Database model for processed entity log - stores resolved sentences from entity_resolver
"""

from sqlalchemy import Column, String, Text, DateTime, Boolean, func, Index
from app.models.base import Base


class ProcessedEntityLog(Base):
    """
    Stores resolved sentences from entity_resolver agent
    Each resolved sentence is linked back to its original unified_log message
    """
    __tablename__ = 'processed_entity_log'
    
    # Primary key
    id = Column(String, primary_key=True)  # Generated UUID
    
    # Link to original message from unified_log
    original_message_id = Column(String, nullable=False)  # ID from unified_log table
    original_message_timestamp = Column(DateTime(timezone=True), nullable=False)
    role = Column(String, nullable=True)  # Role from original message (user, assistant, etc.)
    
    # Resolved sentence data
    original_sentence = Column(Text, nullable=False)  # Original sentence from the message
    resolved_sentence = Column(Text, nullable=False)  # Entity-resolved sentence
    reasoning = Column(Text, nullable=True)  # Agent's reasoning for resolution
    
    # Processing metadata
    processing_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    agent_version = Column(String(50), nullable=True)  # Version of entity_resolver agent
    
    # Status tracking
    processed = Column(Boolean, default=False, nullable=False)  # Whether this has been processed by KG pipeline
    
    __table_args__ = (
        Index('ix_processed_entity_log_original_message_id', 'original_message_id'),
        Index('ix_processed_entity_log_processing_timestamp', 'processing_timestamp'),
        Index('ix_processed_entity_log_processed', 'processed'),
    )
