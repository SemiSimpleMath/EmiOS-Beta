"""
KG Review Data Model
SQLite Compatible Version

Unified model for all knowledge graph analysis findings that require human review.
Consolidates findings from repair pipeline, explorer, maintenance, and other sources.
"""

from sqlalchemy import Column, String, Text, DateTime, Boolean, Float, JSON, Index, Enum
from datetime import datetime, timezone
import uuid
import enum

from app.models.base import Base

# Helper to generate string UUIDs for SQLite
def generate_uuid():
    return str(uuid.uuid4())


class ReviewSource(str, enum.Enum):
    """Source of the KG review"""
    REPAIR_PIPELINE = "repair_pipeline"
    EXPLORER = "explorer"
    MAINTENANCE = "maintenance"
    MANUAL = "manual"
    CUSTOM = "custom"


class ReviewStatus(str, enum.Enum):
    """Status of the KG review"""
    PENDING = "pending"           # Awaiting review
    UNDER_REVIEW = "under_review" # Currently being reviewed
    APPROVED = "approved"         # Approved for implementation
    REJECTED = "rejected"         # Rejected - no action needed
    IMPLEMENTING = "implementing" # Being implemented by kg_team
    COMPLETED = "completed"       # Successfully implemented
    FAILED = "failed"            # Implementation failed
    POSTPONED = "postponed"      # Postponed to later


class ReviewPriority(str, enum.Enum):
    """Priority of the KG review"""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class FindingType(str, enum.Enum):
    """Type of finding/problem identified"""
    MISSING_DATA = "missing_data"           # Node missing required data
    ORPHANED = "orphaned"                   # Node disconnected from graph
    STRUCTURAL = "structural"               # Structural issues
    QUALITY = "quality"                     # Data quality issues
    DUPLICATE = "duplicate"                 # Potential duplicate
    INCONSISTENT = "inconsistent"           # Inconsistent data
    OUTDATED = "outdated"                   # Outdated information
    SUSPICIOUS = "suspicious"               # Suspicious/anomalous node
    OTHER = "other"                         # Other type


class KGReview(Base):
    """
    Unified model for knowledge graph review items.
    
    Stores findings from various KG analysis tools (repair pipeline, explorer, etc.)
    along with user notes and implementation instructions for bulk processing.
    """
    __tablename__ = 'kg_reviews'
    
    # Primary identification
    id = Column(Text, primary_key=True, default=generate_uuid)  # SQLite: UUID as TEXT
    
    # Source and categorization
    source = Column(String, nullable=False, index=True)  # repair_pipeline, explorer, maintenance
    finding_type = Column(String, nullable=True, index=True)  # missing_data, orphaned, quality, etc.
    
    # Node information
    node_id = Column(Text, nullable=False, index=True)  # SQLite: UUID as TEXT
    node_label = Column(String, nullable=True)  # Cached for display
    node_type = Column(String, nullable=True)   # Cached for display
    node_category = Column(String, nullable=True)  # Cached for display
    
    # Problem identification
    problem_description = Column(Text, nullable=False)  # What's wrong
    problem_severity = Column(String, nullable=True)    # critical, high, medium, low
    
    # Analysis results
    analyzer_suggestion = Column(Text, nullable=True)    # Suggestion from analyzer
    critic_suggestion = Column(Text, nullable=True)      # Suggestion from critic
    automated_fix = Column(JSON, nullable=True)          # SQLite: Automated fix if available
    
    # User review
    user_notes = Column(Text, nullable=True)             # User's notes during review
    user_instructions = Column(Text, nullable=True)      # Instructions for kg_team
    reviewed_by = Column(String, nullable=True)          # Who reviewed it
    reviewed_at = Column(DateTime(timezone=True), nullable=True)  # When reviewed
    
    # Status and workflow
    status = Column(String, nullable=False, default='pending', index=True)
    priority = Column(String, nullable=False, default='medium', index=True)
    
    # Implementation tracking
    implementation_started_at = Column(DateTime(timezone=True), nullable=True)
    implementation_completed_at = Column(DateTime(timezone=True), nullable=True)
    implementation_result = Column(Text, nullable=True)  # Result from kg_team
    implementation_error = Column(Text, nullable=True)   # Error if failed
    kg_team_session_id = Column(String, nullable=True)   # Link to kg_team execution
    
    # Metrics and context
    confidence_score = Column(Float, nullable=True)      # Confidence in the finding (0.0-1.0)
    edge_count = Column(Float, nullable=True)            # Number of edges on the node
    importance_score = Column(Float, nullable=True)      # Importance of fixing this
    
    # Additional context
    context_data = Column(JSON, nullable=True)           # SQLite: Full context from analyzer
    related_reviews = Column(JSON, nullable=True)        # SQLite: Related review IDs
    
    # Source tracking
    source_pipeline_id = Column(String, nullable=True)   # Pipeline run that created this
    source_batch_id = Column(String, nullable=True)      # Batch ID if applicable
    
    # Flags
    is_false_positive = Column(Boolean, default=False)   # Marked as false positive
    requires_user_input = Column(Boolean, default=False) # Needs user input before implementation
    is_urgent = Column(Boolean, default=False)           # Urgent flag
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Additional data
    tags = Column(JSON, nullable=True)                   # SQLite: Custom tags for filtering
    extra_metadata = Column(JSON, nullable=True)         # SQLite: Additional metadata
    
    # Indexes for efficient querying
    __table_args__ = (
        Index('ix_kg_reviews_status_priority', 'status', 'priority'),
        Index('ix_kg_reviews_source_status', 'source', 'status'),
        Index('ix_kg_reviews_node_id_status', 'node_id', 'status'),
        Index('ix_kg_reviews_created_at', 'created_at'),
    )
    
    def __repr__(self):
        return f"<KGReview(id={self.id}, node_label='{self.node_label}', status='{self.status}', priority='{self.priority}')>"
    
    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            'id': str(self.id),
            'source': self.source,
            'finding_type': self.finding_type,
            'node_id': str(self.node_id),
            'node_label': self.node_label,
            'node_type': self.node_type,
            'node_category': self.node_category,
            'problem_description': self.problem_description,
            'problem_severity': self.problem_severity,
            'analyzer_suggestion': self.analyzer_suggestion,
            'critic_suggestion': self.critic_suggestion,
            'user_notes': self.user_notes,
            'user_instructions': self.user_instructions,
            'status': self.status,
            'priority': self.priority,
            'confidence_score': self.confidence_score,
            'edge_count': self.edge_count,
            'importance_score': self.importance_score,
            'is_false_positive': self.is_false_positive,
            'requires_user_input': self.requires_user_input,
            'is_urgent': self.is_urgent,
            'reviewed_by': self.reviewed_by,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'implementation_result': self.implementation_result,
            'implementation_error': self.implementation_error,
            'tags': self.tags,
            'extra_metadata': self.extra_metadata,
        }

