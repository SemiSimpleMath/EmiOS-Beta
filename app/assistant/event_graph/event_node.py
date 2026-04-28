"""
EventNode - Semantic Hub for Hierarchical Events
=================================================

Provides a unified layer to connect events across different systems
(Google Calendar, Scheduler, Google Tasks, Goals) with tree-structured
relationships and cascade semantics.

Key Features:
- Tree/DAG structure via parent_event_node_id
- Fast subtree queries via cached root_event_node_id
- Relative timing via offset_from_parent
- Cascade intent tracking (desired_state, cascade_status)
- Orphan detection and handling
"""

from enum import Enum
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, DateTime, Text, Integer, Boolean, 
    ForeignKey, Index, func, JSON
)
from sqlalchemy.orm import relationship
from app.models.base import Base


class NodeKind(str, Enum):
    """Type of node in the event graph."""
    ROOT_EVENT = "root_event"      # Top-level event (Cruise 2025, Project X)
    SUB_EVENT = "sub_event"        # Child event (Packing Day, Milestone)
    REMINDER = "reminder"          # Reminder attached to an event
    GOAL = "goal"                  # User goal
    PROJECT = "project"            # Project container
    TASK = "task"                  # Task/todo item


class DesiredState(str, Enum):
    """Intent for what state this node should be in."""
    ACTIVE = "active"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"
    COMPLETED = "completed"


class CascadeStatus(str, Enum):
    """Status of cascade operation propagation."""
    NONE = "none"                  # No cascade operation pending
    PENDING = "pending"            # Cascade queued but not started
    IN_PROGRESS = "in_progress"    # Cascade currently running
    COMPLETED = "completed"        # Cascade finished successfully
    FAILED = "failed"              # Cascade failed (some children may be inconsistent)
    PARTIAL = "partial"            # Some children updated, some failed


class EventNode(Base):
    """
    Semantic hub representing any event in the system.
    
    Forms a tree structure where:
    - Root nodes have parent_event_node_id = NULL
    - Child nodes reference their parent
    - Reminders reference the event they're attached to
    
    Source systems (Google Calendar, Scheduler, etc.) are linked via
    EventNodeSource table, allowing one logical event to exist in
    multiple systems.
    """
    __tablename__ = 'event_nodes'
    
    # ===== Identity =====
    id = Column(Integer, primary_key=True)
    node_id = Column(String(100), unique=True, nullable=False, index=True)
    
    # ===== Tree Structure =====
    parent_event_node_id = Column(Integer, ForeignKey('event_nodes.id'), nullable=True, index=True)
    root_event_node_id = Column(Integer, ForeignKey('event_nodes.id'), nullable=True, index=True)
    # For root nodes: root_event_node_id = id (self-reference)
    # For children: root_event_node_id = ancestor's root
    
    # Relationships
    parent = relationship("EventNode", remote_side=[id], foreign_keys=[parent_event_node_id], backref="children")
    
    # ===== Node Classification =====
    node_kind = Column(String(50), nullable=False, default=NodeKind.SUB_EVENT.value)
    # root_event, sub_event, reminder, goal, project, task
    
    # ===== Event Info (denormalized for quick access) =====
    title = Column(String(500))
    description = Column(Text)
    
    # ===== Timing =====
    # Absolute times (denormalized from source, updated on cascade)
    start_time = Column(DateTime(timezone=True))
    end_time = Column(DateTime(timezone=True))
    
    # Relative timing for cascades
    offset_from_parent_minutes = Column(Integer, nullable=True)
    # e.g., -1440 = 1 day before parent, +60 = 1 hour after parent
    offset_anchor = Column(String(20), default='start')
    # 'start' = offset from parent's start_time
    # 'end' = offset from parent's end_time
    
    # ===== State & Cascade =====
    desired_state = Column(String(50), default=DesiredState.ACTIVE.value)
    # What state this node should be in (active, cancelled, rescheduled)
    
    cascade_status = Column(String(50), default=CascadeStatus.NONE.value)
    # Status of any pending cascade operation
    
    cascade_error = Column(Text, nullable=True)
    # Error message if cascade failed
    
    # ===== Orphan Handling =====
    orphaned = Column(Boolean, default=False)
    orphan_reason = Column(String(100), nullable=True)
    # 'deleted', 'access_denied', 'moved', 'not_found'
    
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    # When we last verified this node against source systems
    
    # ===== Metadata =====
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    created_by = Column(String(100))  # 'user', 'emi', 'auto_planner'
    
    # ===== Additional Data =====
    metadata_json = Column(JSON, default=dict)
    # Flexible storage for node-specific data
    
    # ===== Indexes =====
    __table_args__ = (
        Index('idx_node_parent', 'parent_event_node_id'),
        Index('idx_node_root', 'root_event_node_id'),
        Index('idx_node_kind', 'node_kind'),
        Index('idx_node_state', 'desired_state'),
        Index('idx_node_orphaned', 'orphaned'),
        Index('idx_node_cascade', 'cascade_status'),
    )
    
    def __repr__(self):
        return f"<EventNode(id={self.id}, node_id='{self.node_id}', kind='{self.node_kind}', title='{self.title[:30] if self.title else 'N/A'}')>"
    
    def to_dict(self):
        """Convert to dictionary for serialization."""
        return {
            'id': self.id,
            'node_id': self.node_id,
            'parent_event_node_id': self.parent_event_node_id,
            'root_event_node_id': self.root_event_node_id,
            'node_kind': self.node_kind,
            'title': self.title,
            'description': self.description,
            'start_time': self.start_time.isoformat() if self.start_time else None,
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'offset_from_parent_minutes': self.offset_from_parent_minutes,
            'offset_anchor': self.offset_anchor,
            'desired_state': self.desired_state,
            'cascade_status': self.cascade_status,
            'cascade_error': self.cascade_error,
            'orphaned': self.orphaned,
            'orphan_reason': self.orphan_reason,
            'last_synced_at': self.last_synced_at.isoformat() if self.last_synced_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'created_by': self.created_by,
            'metadata': self.metadata_json,
        }
    
    def is_root(self) -> bool:
        """Check if this is a root node."""
        return self.parent_event_node_id is None
    
    def get_depth(self) -> int:
        """Get depth in tree (root = 0)."""
        depth = 0
        node = self
        while node.parent_event_node_id is not None:
            depth += 1
            node = node.parent
        return depth


class EventNodeSource(Base):
    """
    Links an EventNode to its representation in source systems.
    
    One EventNode can have multiple sources (e.g., same Cruise event
    exists in Google Calendar AND as internal Scheduler reminders).
    """
    __tablename__ = 'event_node_sources'
    
    id = Column(Integer, primary_key=True)
    
    # ===== Link to EventNode =====
    event_node_id = Column(Integer, ForeignKey('event_nodes.id'), nullable=False, index=True)
    event_node = relationship("EventNode", backref="sources")
    
    # ===== Source System Reference =====
    source_system = Column(String(50), nullable=False)
    # 'google_calendar', 'google_tasks', 'scheduler', 'internal'
    
    source_type = Column(String(50), nullable=False)
    # 'calendar', 'todo', 'reminder', 'goal'
    
    source_id = Column(String(200), nullable=False)
    # ID in the source system (Google Calendar event ID, scheduler ID, etc.)
    
    # ===== Source State =====
    is_primary = Column(Boolean, default=True)
    # If multiple sources, which is the primary/authoritative one
    
    last_seen_at = Column(DateTime(timezone=True))
    # When we last verified this source exists
    
    sync_status = Column(String(50), default='synced')
    # 'synced', 'pending_update', 'pending_delete', 'error', 'orphaned'
    
    sync_error = Column(Text, nullable=True)
    
    # ===== Metadata =====
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    
    # ===== Indexes =====
    __table_args__ = (
        Index('idx_source_node', 'event_node_id'),
        Index('idx_source_system_id', 'source_system', 'source_id'),
        Index('idx_source_type', 'source_type'),
    )
    
    def __repr__(self):
        return f"<EventNodeSource(node_id={self.event_node_id}, system='{self.source_system}', source_id='{self.source_id}')>"
    
    def to_dict(self):
        return {
            'id': self.id,
            'event_node_id': self.event_node_id,
            'source_system': self.source_system,
            'source_type': self.source_type,
            'source_id': self.source_id,
            'is_primary': self.is_primary,
            'last_seen_at': self.last_seen_at.isoformat() if self.last_seen_at else None,
            'sync_status': self.sync_status,
            'sync_error': self.sync_error,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ===== Database Management Functions =====

def initialize_event_graph_db():
    """Initialize event_nodes and event_node_sources tables."""
    from app.models.base import get_session
    session = get_session()
    engine = session.bind
    print(f"🔍 Event Graph DB: Connecting to database: {engine.url}")
    Base.metadata.create_all(engine, tables=[EventNode.__table__, EventNodeSource.__table__], checkfirst=True)
    session.close()
    print("✅ event_nodes and event_node_sources tables initialized successfully.")


def drop_event_graph_db():
    """Drop event_nodes and event_node_sources tables."""
    from app.models.base import get_session
    session = get_session()
    engine = session.bind
    Base.metadata.drop_all(engine, tables=[EventNodeSource.__table__, EventNode.__table__], checkfirst=True)
    session.close()
    print("✅ Event graph tables dropped successfully.")


def reset_event_graph_db():
    """Drop and recreate event graph tables."""
    print("⚠️  Resetting event graph tables...")
    drop_event_graph_db()
    initialize_event_graph_db()
    print("✅ Event graph tables reset completed.")


if __name__ == "__main__":
    print("\n🚀 Initializing event graph tables...")
    initialize_event_graph_db()
    print("\n💡 Tables created successfully!")

