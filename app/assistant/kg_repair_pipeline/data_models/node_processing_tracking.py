"""
Node Processing Tracking Database Model
SQLite Compatible Version

Tracks the status of nodes in the KG repair pipeline, including user interactions,
suggestions, and scheduling information.
"""

from sqlalchemy import Column, String, Text, DateTime, Boolean, Integer, Float, JSON, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid

from app.models.base import Base

# Helper to generate string UUIDs for SQLite
def generate_uuid():
    return str(uuid.uuid4())

class NodeProcessingStatus(Base):
    """
    Tracks the processing status of nodes in the KG repair pipeline.
    Stores only essential state information that cannot be retrieved from the KG.
    """
    __tablename__ = 'node_processing_status'
    
    # Primary key
    id = Column(Text, primary_key=True, default=generate_uuid)  # SQLite: UUID as TEXT
    
    # Node identification (minimal - only what we can't get from KG)
    node_id = Column(Text, nullable=False, index=True)  # SQLite: UUID as TEXT
    
    # Problem identification
    problem_description = Column(Text, nullable=True)  # What problem was identified
    problem_category = Column(String, nullable=True)  # missing_data, orphaned, structural, content, etc.
    
    # Pipeline position
    current_stage = Column(String, nullable=False, default='pending')  # pending, questioned, implementing, completed, error
    last_action = Column(Text, nullable=True)  # What we did last
    next_action = Column(Text, nullable=True)  # What we need to do next
    
    # User interaction
    user_response = Column(Text, nullable=True)  # Raw user response
    user_response_type = Column(String, nullable=True)  # provide_data, skip, ask_later, no_idea, invalid
    user_provided_data = Column(JSON, nullable=True)  # SQLite: Structured data from user
    user_instructions = Column(Text, nullable=True)  # What user told us to do
    
    # Implementation tracking
    implementation_instructions = Column(JSON, nullable=True)  # SQLite: Generated implementation instructions
    implementation_status = Column(String, nullable=True)  # pending, in_progress, completed, failed
    implementation_result = Column(Text, nullable=True)  # Result of implementation
    error_details = Column(Text, nullable=True)  # Error message if implementation failed
    
    # Scheduling and timing
    first_identified_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now(timezone.utc))
    last_offered_at = Column(DateTime(timezone=True), nullable=True)  # When last offered to user
    next_review_at = Column(DateTime(timezone=True), nullable=True)  # When to offer again
    resolved_at = Column(DateTime(timezone=True), nullable=True)  # When resolved
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))
    
    # Retry and attempt tracking
    attempt_count = Column(Integer, nullable=False, default=0)  # Number of times offered to user
    max_attempts = Column(Integer, nullable=True, default=3)  # Maximum attempts before giving up
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)  # When last attempted
    
    # Learning and quality
    is_false_positive = Column(Boolean, nullable=False, default=False)  # Was this a false positive?
    user_marked_invalid = Column(Boolean, nullable=False, default=False)  # User said this isn't a problem
    should_skip_future = Column(Boolean, nullable=False, default=False)  # Skip in future analysis
    learning_notes = Column(Text, nullable=True)  # Notes for improving future analysis
    
    # Pipeline context
    pipeline_id = Column(String, nullable=True)  # Which pipeline run identified this
    batch_id = Column(String, nullable=True)  # Which batch this was processed in
    processing_agent = Column(String, nullable=True)  # Which agent processed this
    
    # Metadata
    notes = Column(Text, nullable=True)  # General notes
    additional_data = Column(JSON, nullable=True)  # SQLite: Additional metadata
    
    # Indexes for efficient querying
    __table_args__ = (
        # Composite index for common queries (current_stage + should_skip_future)
        Index('ix_node_processing_status_stage_skip', 'current_stage', 'should_skip_future'),
    )

class NodeProcessingBatch(Base):
    """
    Tracks batches of node processing for pipeline runs.
    """
    __tablename__ = 'node_processing_batches'
    
    # Primary key
    id = Column(Text, primary_key=True, default=generate_uuid)  # SQLite: UUID as TEXT
    batch_id = Column(String, nullable=False, unique=True, index=True)
    
    # Batch information
    pipeline_id = Column(String, nullable=False, index=True)
    batch_type = Column(String, nullable=False)  # new_nodes, random_existing, scheduled_retry, etc.
    total_nodes = Column(Integer, nullable=False, default=0)
    processed_nodes = Column(Integer, nullable=False, default=0)
    problematic_nodes = Column(Integer, nullable=False, default=0)
    resolved_nodes = Column(Integer, nullable=False, default=0)
    
    # Timing
    started_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now(timezone.utc))
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    
    # Status
    status = Column(String, nullable=False, default='running')  # running, completed, failed, cancelled
    error_message = Column(Text, nullable=True)
    
    # Configuration
    max_nodes = Column(Integer, nullable=True)
    batch_config = Column(JSON, nullable=True)  # SQLite
    
    # Metadata
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))

class NodeProcessingStatistics(Base):
    """
    Aggregated statistics for node processing performance and learning.
    """
    __tablename__ = 'node_processing_statistics'
    
    # Primary key
    id = Column(Text, primary_key=True, default=generate_uuid)  # SQLite: UUID as TEXT
    
    # Time period
    date = Column(DateTime(timezone=True), nullable=False, index=True)
    period_type = Column(String, nullable=False)  # daily, weekly, monthly
    
    # Processing statistics
    total_nodes_processed = Column(Integer, nullable=False, default=0)
    problematic_nodes_found = Column(Integer, nullable=False, default=0)
    nodes_resolved = Column(Integer, nullable=False, default=0)
    nodes_skipped = Column(Integer, nullable=False, default=0)
    nodes_scheduled = Column(Integer, nullable=False, default=0)
    false_positives = Column(Integer, nullable=False, default=0)
    
    # User interaction statistics
    user_responses_received = Column(Integer, nullable=False, default=0)
    user_provided_data = Column(Integer, nullable=False, default=0)
    user_skipped = Column(Integer, nullable=False, default=0)
    user_asked_later = Column(Integer, nullable=False, default=0)
    user_no_idea = Column(Integer, nullable=False, default=0)
    user_marked_invalid = Column(Integer, nullable=False, default=0)
    
    # Performance metrics
    average_processing_time = Column(Float, nullable=True)  # seconds
    success_rate = Column(Float, nullable=True)  # 0.0-1.0
    user_satisfaction = Column(Float, nullable=True)  # 0.0-1.0
    
    # Learning metrics
    false_positive_rate = Column(Float, nullable=True)  # 0.0-1.0
    improvement_suggestions = Column(Text, nullable=True)
    
    # Metadata
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc))
