"""
SQLAlchemy models for the clean taxonomy system.

This module defines:
- Taxonomy: Hierarchical TYPE concepts only (animal, dog, ownership)
- NodeTaxonomyLink: Many-to-many classification between nodes and types

Design principles:
- Taxonomy stores TYPES, not instances
- Pure adjacency list (id + parent_id)
- No materialized path (use recursive CTEs)
- Instances are nodes that link to taxonomy via node_taxonomy_links
- Progressive classification: nodes can be unclassified initially

Example:
  taxonomy: id=888, label="dog", parent_id=100 (animal)
  nodes: Scout (UUID), Buddy (UUID)
  node_taxonomy_links: Scout→888, Buddy→888
"""

from sqlalchemy import Column, Integer, String, Text, Float, ForeignKey, DateTime, JSON
from sqlalchemy.orm import relationship
from datetime import datetime

from app.models.base import Base

# SQLite version: Uses JSON fields, embeddings stored in ChromaDB

# Forward reference for Node - actual import would cause circular dependency
# The relationship will be resolved at runtime when Node is defined


class Taxonomy(Base):
    """
    Hierarchical taxonomy for TYPE concepts only.
    
    This table stores:
    - Categories/types (animal, dog, poodle)
    - States/relationships (ownership, employment)
    - Hierarchical structure via parent_id
    
    It does NOT store:
    - Instances (Scout, Buddy) - those are nodes
    - Materialized paths - computed via recursive CTEs
    
    Examples:
    - id=100, label="animal", parent_id=NULL
    - id=888, label="dog", parent_id=100
    - id=123, label="ownership", parent_id=<state_id>
    """
    __tablename__ = 'taxonomy'
    
    # Primary key
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Type identity
    label = Column(String(255), nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey('taxonomy.id', ondelete='RESTRICT'), nullable=True, index=True)
    
    # Optional description
    description = Column(Text, nullable=True)
    
    # Note: Embeddings are stored in ChromaDB for SQLite compatibility
    # label_embedding moved to ChromaDB collection 'taxonomy_embeddings'
    
    # Metadata
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    parent = relationship('Taxonomy', remote_side=[id], backref='children')
    
    __table_args__ = {'extend_existing': True}
    
    @property
    def label_embedding(self):
        """
        Get label embedding from ChromaDB.
        If not found, compute and store it.
        """
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils
        from app.models.base import get_session
        
        chroma = get_chroma_manager()
        
        # Try to get from ChromaDB first
        embedding = chroma.get_taxonomy_embedding(self.id)
        
        # If not found, compute and store
        if embedding is None:
            session = get_session()
            try:
                kg_utils = KnowledgeGraphUtils(session)
                embedding = kg_utils.create_embedding(self.label)
                chroma.store_taxonomy_embedding(self.id, self.label, embedding)
            finally:
                session.close()  # Always close the session!
        
        return embedding
    
    def __repr__(self):
        return f"<Taxonomy(id={self.id}, label='{self.label}', parent_id={self.parent_id})>"
    
    def get_ancestors(self, session):
        """Get all ancestor types from root to parent using recursive query."""
        from sqlalchemy import text
        result = session.execute(text("""
            WITH RECURSIVE ancestors AS (
                SELECT id, label, parent_id
                FROM taxonomy
                WHERE id = :current_id
                
                UNION ALL
                
                SELECT t.id, t.label, t.parent_id
                FROM taxonomy t
                INNER JOIN ancestors a ON t.id = a.parent_id
            )
            SELECT id, label, parent_id FROM ancestors WHERE id != :current_id
            ORDER BY id;
        """), {"current_id": self.id})
        
        return [session.query(Taxonomy).get(row[0]) for row in result]
    
    def get_descendants(self, session):
        """Get all descendant types using recursive query."""
        from sqlalchemy import text
        result = session.execute(text("""
            WITH RECURSIVE descendants AS (
                SELECT id, label, parent_id
                FROM taxonomy
                WHERE id = :current_id
                
                UNION ALL
                
                SELECT t.id, t.label, t.parent_id
                FROM taxonomy t
                INNER JOIN descendants d ON t.parent_id = d.id
            )
            SELECT id, label, parent_id FROM descendants WHERE id != :current_id;
        """), {"current_id": self.id})
        
        return [session.query(Taxonomy).get(row[0]) for row in result]
    
    def get_siblings(self, session):
        """Get all sibling types (same parent)."""
        if self.parent_id is None:
            # Root concepts
            return session.query(Taxonomy).filter(
                Taxonomy.parent_id.is_(None),
                Taxonomy.id != self.id
            ).all()
        else:
            return session.query(Taxonomy).filter(
                Taxonomy.parent_id == self.parent_id,
                Taxonomy.id != self.id
            ).all()
    
    def get_path(self, session):
        """Compute materialized path on demand (e.g., 'animal.dog.poodle')."""
        ancestors = self.get_ancestors(session)
        path_parts = [a.label for a in ancestors] + [self.label]
        return ".".join(path_parts)


class NodeTaxonomyLink(Base):
    """
    Many-to-many classification between Nodes and Taxonomy types.
    
    A single node can have multiple taxonomy types:
    - Entity node: Scout (UUID) → [dog, pet, male] (multiple types)
    - State node: "Alex owns Scout" → [ownership] (relationship type)
    
    Progressive classification:
    - Initially: Scout node exists but has no taxonomy links
    - Later: Learn "Scout is a dog" → add link to 888 (dog) with confidence=1.0
    - Later: Infer "Scout is a pet" → add link with confidence=0.8
    - All past references to Scout benefit from new classifications!
    
    Confidence levels:
    - 1.0: Explicit statement ("Scout is a dog")
    - 0.8: Strong contextual evidence ("Scout barked")
    - 0.6: Accumulated weak evidence (multiple hints)
    - < 0.6: Too uncertain, don't assign
    """
    __tablename__ = 'node_taxonomy_links'
    
    # Composite primary key
    # SQLite: UUIDs stored as TEXT (STRING)
    node_id = Column(String, ForeignKey('kg_node_metadata.id', ondelete='CASCADE'), primary_key=True)
    taxonomy_id = Column(Integer, ForeignKey('taxonomy.id', ondelete='RESTRICT'), primary_key=True)
    
    # Classification metadata
    confidence = Column(Float, default=1.0, nullable=False)
    source = Column(String(100), nullable=True)  # "explicit", "strong_context", "accumulated", "inferred", "manual"
    count = Column(Integer, default=1, nullable=False)  # How many times this classification has been made
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    last_seen = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=True)  # Most recent classification timestamp
    
    # Relationships (using lazy string reference to avoid circular import)
    # The 'Node' relationship will be resolved when the Node model is imported
    taxonomy_type = relationship('Taxonomy', backref='node_links')
    
    __table_args__ = {'extend_existing': True}
    
    def __repr__(self):
        return f"<NodeTaxonomyLink(node_id={self.node_id}, taxonomy_id={self.taxonomy_id}, confidence={self.confidence})>"


class TaxonomySuggestion(Base):
    """
    Tracks suggestions for new taxonomy types from the classifier agent.
    
    When the classifier encounters a concept that doesn't exist in the taxonomy,
    it suggests what type should be created. This table captures those suggestions
    so the researcher agent (Phase 2) can analyze patterns and build out the taxonomy.
    
    Example:
    - Agent classifies "Birthday Party" and suggests "Party" as a new type
    - Record created with suggested_type="Party", parent_candidate_id=156 (social)
    - If "Party" is suggested multiple times, count is incremented
    - Researcher agent later creates: event → social → party
    """
    __tablename__ = 'taxonomy_suggestions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # What type was suggested
    suggested_type = Column(String(255), nullable=False, index=True)
    
    # Context that triggered the suggestion
    node_label = Column(String(255), nullable=False)
    node_sentence = Column(Text, nullable=True)
    node_type = Column(String(50), nullable=True)  # Entity, Event, State, etc.
    
    # Where it might fit in the taxonomy
    parent_candidate_id = Column(Integer, ForeignKey('taxonomy.id', ondelete='SET NULL'), nullable=True)
    match_quality = Column(Integer, nullable=True)  # 1-10 rating from agent
    
    # Tracking
    count = Column(Integer, default=1, nullable=False)  # Increments if seen again
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    parent_candidate = relationship('Taxonomy', backref='suggestions')
    
    __table_args__ = {'extend_existing': True}
    
    def __repr__(self):
        return f"<TaxonomySuggestion(suggested_type='{self.suggested_type}', count={self.count})>"


class TaxonomySuggestions(Base):
    """
    Review queue for new taxonomy type suggestions from the critic agent.
    
    When the critic decides a node needs a new taxonomy type (VALIDATE_INSERT action),
    it creates a suggestion record here for human review before adding to the taxonomy.
    
    This uses a different schema than TaxonomySuggestion for the proposer-critic workflow.
    
    Example:
    - Critic reviews "Software Developer" classification
    - Decides it needs "software_developer" under "entity > person > occupation"
    - Creates suggestion with parent_path, suggested_label, reasoning, example nodes
    - Human reviews and either approves (creates taxonomy entry) or rejects
    """
    __tablename__ = 'taxonomy_suggestions_review'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Where the new type should be inserted
    parent_path = Column(Text, nullable=False, index=True)  # e.g., "entity > person > occupation"
    suggested_label = Column(String(255), nullable=False, index=True)  # e.g., "software_developer"
    
    # Why this type is needed
    description = Column(Text, nullable=True)
    reasoning = Column(Text, nullable=True)
    
    # Example nodes that would fit this type
    example_nodes = Column(JSON, nullable=True)  # List of {node_id, label, sentence}
    
    # Classification confidence
    confidence = Column(Float, nullable=True)
    
    # Review status
    status = Column(String(50), default='pending', nullable=False, index=True)  # pending, approved, rejected
    reviewed_by = Column(String(255), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    
    # Tracking
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    
    __table_args__ = {'extend_existing': True}
    
    def __repr__(self):
        return f"<TaxonomySuggestions(parent_path='{self.parent_path}', suggested_label='{self.suggested_label}', status='{self.status}')>"


class NodeTaxonomyReviewQueue(Base):
    """
    Review queue for low-confidence or rejected node classifications.
    
    When the critic identifies a classification that needs human review:
    - CORRECT_PATH with low confidence (< 0.85)
    - REJECT action (doesn't fit anywhere)
    
    This allows humans to review and correct the classification before it becomes permanent.
    
    Example:
    - Node "Meeting" classified as "entity > organization > professional_meeting"
    - Critic corrects to "event > professional_event" with confidence 0.70
    - Added to review queue for human confirmation
    """
    __tablename__ = 'node_taxonomy_review_queue'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # Node being reviewed
    # SQLite: UUIDs stored as TEXT (STRING)
    node_id = Column(String, ForeignKey('kg_node_metadata.id', ondelete='CASCADE'), nullable=False, index=True)
    
    # Classification paths
    proposed_path = Column(Text, nullable=True)  # Original proposer classification
    validated_path = Column(Text, nullable=True)  # Critic's correction/validation
    
    # Critic decision
    action = Column(String(50), nullable=False, index=True)  # CORRECT_PATH, REJECT
    confidence = Column(Float, nullable=True)
    reasoning = Column(Text, nullable=True)
    
    # Review status
    status = Column(String(50), default='pending', nullable=False, index=True)  # pending, approved, rejected, corrected
    reviewed_by = Column(String(255), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    final_taxonomy_path = Column(Text, nullable=True)  # Human's final decision
    
    # Tracking
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    
    __table_args__ = {'extend_existing': True}
    
    def __repr__(self):
        return f"<NodeTaxonomyReviewQueue(node_id={self.node_id}, action='{self.action}', status='{self.status}')>"

