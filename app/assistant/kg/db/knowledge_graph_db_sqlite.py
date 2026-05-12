# knowledge_graph_db_sqlite.py
# SQLite + ChromaDB compatible version of KG models

import uuid
from sqlalchemy import (
    Boolean, Column, String, JSON, DateTime, Text, ForeignKey, Float, Integer,
    UniqueConstraint, Index, func
)
from sqlalchemy.orm import relationship
from app.assistant.utils.time_utils import AwareUtcDateTime, utc_now
from app.models.base import Base

# --- Message ID to Source Mapping Table ---
class MessageSourceMapping(Base):
    __tablename__ = 'message_source_mapping'
    message_id = Column(String, primary_key=True)
    source_table = Column(String, nullable=False)
    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    
    __table_args__ = (
        Index('ix_message_source_mapping_source_table', 'source_table'),
        {'extend_existing': True},
    )


# --- Universal Node Table (SQLite version) ---
class Node(Base):
    __tablename__ = 'kg_node_metadata'
    
    # Core fields
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    label = Column(String, nullable=False)
    node_type = Column(String, nullable=False)  # Entity, Event, State, Goal, Concept, Property
    
    # Searchable fields
    description = Column(Text, nullable=True)
    aliases = Column(JSON, nullable=True)  # Stored as JSON array in SQLite
    category = Column(String, nullable=True)
    
    # Flexible attributes
    attributes = Column(JSON, nullable=False, default=dict)
    
    # Provenance: ``original_sentence`` carries the canonical sentence the
    # node represents (present-tense canonical for State/Event/Goal via
    # fact_canonicalizer; raw extractor sentence for Entity/Concept/Property).
    # Per-observation provenance (window_id, source message, derived sentence,
    # merge action) lives in kg_node_evidence — JOIN through node_id rather
    # than denormalizing onto this row. The legacy window_id /
    # original_message_id / sentence_id columns were dropped 2026-05-04 after
    # the post-rebuild evidence backfill (commit ced1d8b7).
    original_sentence = Column(Text, nullable=True)

    # Timestamps
    start_date = Column(AwareUtcDateTime, nullable=True)
    end_date = Column(AwareUtcDateTime, nullable=True)
    start_date_confidence = Column(String, nullable=True)
    end_date_confidence = Column(String, nullable=True)
    # Natural-language descriptions for when exact dates aren't known but a
    # relational anchor exists ("shortly after Sam was born", "during
    # Waldo's last year"). Complements start_date/end_date — either can be
    # populated, neither has to be. A later pass can resolve prose to exact.
    start_date_prose = Column(Text, nullable=True)
    end_date_prose = Column(Text, nullable=True)
    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    updated_at = Column(AwareUtcDateTime, nullable=False, default=utc_now, onupdate=utc_now)
    
    # Promoted fields
    valid_during = Column(Text, nullable=True)  # DEPRECATED 2026-05-12 — superseded by valid_currently; column retained for legacy readers
    hash_tags = Column(JSON, nullable=True)  # Stored as JSON array in SQLite
    semantic_label = Column(String, nullable=True)
    goal_status = Column(String, nullable=True)

    # Validity assessment (2026-05-12). Replaces valid_during for the
    # "is this still in effect" question. Set by meta_data_add for
    # State/Event when bounding evidence exists; by deterministic code
    # for Goal (from goal_status). NULL = no closing evidence (presumed
    # valid). FALSE = explicitly closed. We never stamp TRUE — asymmetric
    # trust, only the negative signal is strong.
    valid_currently = Column(Boolean, nullable=True)
    validity_reason = Column(Text, nullable=True)
    
    # NOTE: Embeddings are stored in ChromaDB, not here

    # First-class fields
    confidence = Column(Float, nullable=True)
    importance = Column(Float, nullable=True)
    source = Column(String, nullable=True)

    # Observation lifecycle (promoted from attributes JSON on 2026-05-11).
    # first_observed = when the claim was first observed in chat — NOT the
    # same as created_at (which is when the node was minted in DB; can lag
    # by weeks if the pipeline runs against older unified_log rows). This
    # is the biographical date for timeline queries — "what happened in
    # March 2025?" filters by first_observed, not created_at.
    first_observed = Column(AwareUtcDateTime, nullable=True)
    # last_observed = when the claim was most recently re-observed. Decay
    # routine queries this to find stale facts. Updated on every match.
    last_observed = Column(AwareUtcDateTime, nullable=True)
    # observation_count = how many times we've seen this claim. Used by
    # dedup heuristics, confidence_tier ladder, decay weighting. Default 1
    # because a node exists iff it's been observed at least once.
    observation_count = Column(Integer, nullable=False, default=1, server_default='1')
    # last_pursued_at = Goal-only lifecycle field; bumped each time the Goal
    # is reasserted in chat. The goal dormancy sweep filters by this to
    # find goals that haven't been mentioned in N days. NULL for non-Goal
    # nodes. Promoted from attributes JSON 2026-05-11.
    last_pursued_at = Column(AwareUtcDateTime, nullable=True)

    # Computed graph metrics — recalculated by KGMaintenancePipeline, never overwritten by extraction
    pagerank_score = Column(Float, nullable=True)

    # Axiom layer: non-NULL means this node is user-locked and immune to
    # automatic override by extraction/merging. Incoming proposals that
    # would contradict a locked node MUST be rejected as findings, never
    # silently applied. Only set by explicit user action (UI or bulk-seed).
    locked_by_user_at = Column(AwareUtcDateTime, nullable=True, index=True)

    # Confidence tier — first-class structural signal for the maintenance loop.
    # Enum (enforced at application layer):
    #   'axiom'       — user-locked, immune to silent override
    #   'confirmed'   — observed >=3 times OR user-touched
    #   'provisional' — observed 1-2 times (default for fresh extraction)
    #   'inferred'    — wiki_inference / not directly observed in chat
    # Investigator / promoter / decay paths key off this column to decide
    # whether to act automatically or route to user review.
    confidence_tier = Column(
        String, nullable=False,
        default='provisional', server_default='provisional',
        index=True,
    )

    # Back-pointer to the claim_proposals row that promoted this node.
    # NULL for pre-proposal-era nodes and bulk-seeded axioms.
    created_from_proposal_id = Column(String, nullable=True, index=True)

    # ORM relationships
    #
    # IMPORTANT:
    # Use local class names + lambda-based FK resolution so module moves/renames
    # don't break mapper initialization (SQLAlchemy evaluates these at configure time).
    # passive_deletes=True is REQUIRED. Without it, SQLAlchemy walks the
    # collection on Node delete and emits UPDATE kg_edge_metadata SET
    # source_id/target_id = NULL — which violates the NOT NULL constraint
    # before the DB-level ON DELETE CASCADE on the FK can fire. With it,
    # the ORM trusts the cascade and the delete works in one shot.
    # passive_deletes=True is preserved but is now a no-op: the DB no longer
    # has ON DELETE CASCADE on Edge.source_id / target_id (FK was dropped in
    # the 2026-05-10 no-mirror migration). Node deletion via raw SQL in
    # node_merge.py is now solely responsible for cleaning up edges first.
    outgoing_edges = relationship(
        "Edge",
        back_populates="source_node",
        foreign_keys=lambda: [Edge.source_id],
        primaryjoin="Node.id == Edge.source_id",
        passive_deletes=True,
    )
    incoming_edges = relationship(
        "Edge",
        back_populates="target_node",
        foreign_keys=lambda: [Edge.target_id],
        primaryjoin="Node.id == Edge.target_id",
        passive_deletes=True,
    )
    
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
        embedding = chroma.get_node_embedding(str(self.id))
        
        # If not found, compute and store
        if embedding is None:
            session = get_session()
            try:
                kg_utils = KnowledgeGraphUtils(session)
                embedding = kg_utils.create_embedding(self.label)
                chroma.store_node_embedding(str(self.id), self.label, embedding)
            finally:
                session.close()  # Always close the session!
        
        return embedding
    
    __table_args__ = (
        Index('ix_kg_nodes_label', 'label'),
        Index('ix_kg_nodes_node_type', 'node_type'),
        Index('ix_kg_nodes_category', 'category'),
        Index('ix_kg_nodes_start_date', 'start_date'),
        Index('ix_kg_nodes_end_date', 'end_date'),
        Index('ix_kg_nodes_confidence', 'confidence'),
        Index('ix_kg_nodes_importance', 'importance'),
        Index('ix_kg_nodes_source', 'source'),
        # Timeline + decay queries hit these constantly.
        Index('ix_kg_nodes_first_observed', 'first_observed'),
        Index('ix_kg_nodes_last_observed', 'last_observed'),
        # Goal dormancy sweep filters by this.
        Index('ix_kg_nodes_last_pursued_at', 'last_pursued_at'),
    )


# --- Section Tag Table -------------------------------------------------
# Per-node section memberships for downstream projections (cards, wiki).
# Written once at promotion time (or backfilled), read by card_builder and
# wiki page builder via a SELECT — neither rebuilds tags at projection time.
#
# Multi-tag per namespace is allowed: one node can belong to multiple wiki
# sections (the existing wiki_section_tagger explicitly supports this), and
# the same node may also carry a card-section tag.
class NodeSectionTag(Base):
    __tablename__ = 'kg_node_section_tag'

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    node_id = Column(String, ForeignKey('kg_node_metadata.id', ondelete='CASCADE'), nullable=False)
    # 'card' (entity cards) | 'wiki' (wiki pages). Future namespaces would slot
    # in cleanly — e.g., 'lens' if /me starts pulling pre-tagged content.
    namespace = Column(String, nullable=False)
    section_name = Column(String, nullable=False)
    tagged_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    # tagger_version pins what produced the tag — bump when the section vocab
    # or tagger prompt changes meaningfully so a backfill can find stale tags.
    tagger_version = Column(String, nullable=False, default='v1')

    # Try-and-mark drop tracking (added 2026-05-11). A node can be tagged into
    # a section but get dropped by the section's renderer/distiller because it
    # lost on relative merit (the section is bullet-capped and other facts
    # ranked higher). We persist that decision so the next refresh doesn't
    # re-pay the LLM cost to re-discover "this fact didn't make the cut."
    #
    # dropped_at: when the build last excluded this tag's node. NULL means
    #   never dropped — always re-evaluated.
    # dropped_at_node_content_hash: hash of the node's claim content at drop
    #   time. If the node's content changes (e.g. original_sentence rewritten,
    #   wiki refreshes description), the hash mismatches and we re-consider.
    # dropped_by_version: builder version string at drop time. Bumping the
    #   version (prompt change, threshold change) triggers re-consideration
    #   for all previously-dropped facts.
    dropped_at = Column(AwareUtcDateTime, nullable=True)
    dropped_at_node_content_hash = Column(String, nullable=True)
    dropped_by_version = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint('node_id', 'namespace', 'section_name', name='uq_kg_node_section_tag'),
        Index('ix_kg_node_section_tag_node', 'node_id'),
        Index('ix_kg_node_section_tag_lookup', 'namespace', 'section_name'),
        Index('ix_kg_node_section_tag_dropped', 'dropped_at'),
    )


# --- Universal Edge Table (SQLite version) ---
class Edge(Base):
    __tablename__ = 'kg_edge_metadata'

    # NOTE: source_id and target_id deliberately have NO ForeignKey to
    # kg_node_metadata. They can also hold pod URIs (``datapod:<kind>:<hash>``),
    # which are valid edge endpoints with no kg_node row by design — pod_store
    # is the sole authority for pod content. The DB-level FK was dropped on
    # 2026-05-10 (the no-mirror migration). The relationships below stay
    # functional via explicit ``primaryjoin`` so callers can still read
    # ``edge.source_node`` / ``edge.target_node`` (which return None for
    # pod-URI endpoints — expected and handled downstream).
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id = Column(String, nullable=False, index=True)
    target_id = Column(String, nullable=False, index=True)
    relationship_type = Column(String, nullable=False)
    attributes = Column(JSON, nullable=False, default=dict)

    # Canonicalized sentence for the edge (mirrors Node.original_sentence
    # contract). Per-observation provenance — source message, raw text,
    # window_id, timestamps — lives in kg_edge_evidence. JOIN through
    # edge_id rather than denormalizing.
    # Removed 2026-05-10 (mirrors the 2026-05-04 node-side cleanup):
    #   - original_message_id    → kg_edge_evidence.source_id
    #   - original_message_timestamp → kg_edge_evidence.message_timestamp
    #   - sentence_id            → no modern equivalent; retired
    #   - window_id              → kg_edge_evidence.window_id
    #   - relationship_descriptor → never populated (0% in production)
    sentence = Column(Text, nullable=True)

    created_at = Column(AwareUtcDateTime, nullable=False, default=utc_now)
    updated_at = Column(AwareUtcDateTime, nullable=False, default=utc_now, onupdate=utc_now)
    
    # NOTE: Embeddings are stored in ChromaDB, not here
    
    # First-class fields
    confidence = Column(Float, nullable=True)
    importance = Column(Float, nullable=True)
    source = Column(String, nullable=True)

    # Axiom layer — see Node.locked_by_user_at. Non-NULL means this edge is
    # user-locked and immune to automatic override.
    locked_by_user_at = Column(AwareUtcDateTime, nullable=True, index=True)

    # Confidence tier — see Node.confidence_tier. Same enum + semantics.
    confidence_tier = Column(
        String, nullable=False,
        default='provisional', server_default='provisional',
        index=True,
    )

    # Back-pointer to the claim_proposals row that promoted this edge.
    created_from_proposal_id = Column(String, nullable=True, index=True)

    # ORM relationships. Explicit primaryjoin is required because source_id/
    # target_id are no longer ForeignKey columns (see class comment above).
    # Returns None for pod-URI endpoints — callers handle.
    source_node = relationship(
        "Node",
        back_populates="outgoing_edges",
        foreign_keys=[source_id],
        primaryjoin="Edge.source_id == Node.id",
    )
    target_node = relationship(
        "Node",
        back_populates="incoming_edges",
        foreign_keys=[target_id],
        primaryjoin="Edge.target_id == Node.id",
    )
    
    @property
    def sentence_embedding(self):
        """
        Get sentence embedding from ChromaDB.
        If not found, compute and store it.
        """
        if not self.sentence:
            return None
        
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils
        from app.models.base import get_session
        
        chroma = get_chroma_manager()
        
        # Try to get from ChromaDB first
        embedding = chroma.get_edge_embedding(str(self.id))
        
        # If not found, compute and store
        if embedding is None:
            session = get_session()
            try:
                kg_utils = KnowledgeGraphUtils(session)
                embedding = kg_utils.create_embedding(self.sentence)
                chroma.store_edge_embedding(str(self.id), self.sentence, embedding)
            finally:
                session.close()  # Always close the session!
        
        return embedding
    
    __table_args__ = (
        UniqueConstraint('source_id', 'target_id', 'relationship_type', name='uq_edge_unique'),
        Index('ix_kg_edges_sentence', 'sentence'),
        Index('ix_kg_edges_confidence', 'confidence'),
        Index('ix_kg_edges_importance', 'importance'),
        Index('ix_kg_edges_source', 'source'),
    )


# Node type constants for compatibility. 'Pod' is intentionally NOT included
# here — pods are addressed by URI directly in kg_edge_metadata.source_id /
# target_id and have no kg_node_metadata rows.
NODE_TYPES = ['Entity', 'Event', 'State', 'Goal', 'Concept', 'Property']


# --- Database Management Functions ---
def initialize_knowledge_graph_db():
    """Initialize knowledge graph tables."""
    from app.models.base import get_session
    session = get_session()
    engine = session.bind
    print(f"🔍 KG DB Debug: Connecting to database: {engine.url}")
    Base.metadata.create_all(engine, checkfirst=True)
    session.close()
    print("Knowledge graph tables initialized successfully.")


def drop_knowledge_graph_db():
    """Drop all knowledge graph tables."""
    from app.models.base import get_session
    session = get_session()
    engine = session.bind
    Base.metadata.drop_all(engine, tables=[
        Edge.__table__, Node.__table__, MessageSourceMapping.__table__,
    ], checkfirst=True)
    session.close()
    print("Knowledge graph tables dropped successfully.")


def reset_knowledge_graph_db():
    """Drop and recreate all knowledge graph tables."""
    print("Resetting knowledge graph database...")
    drop_knowledge_graph_db()
    initialize_knowledge_graph_db()
    print("Knowledge graph database reset completed.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        command = sys.argv[1].lower()
        if command == "drop":
            drop_knowledge_graph_db()
        elif command == "reset":
            reset_knowledge_graph_db()
        elif command == "init":
            initialize_knowledge_graph_db()
        else:
            print("Usage: python knowledge_graph_db_sqlite.py [init|drop|reset]")
    else:
        initialize_knowledge_graph_db()
