#knowledge_graph_utils.py
import uuid
import json
from typing import List, Optional, Tuple, Dict, Any
from sqlalchemy.orm import Session
import numpy as np
from datetime import datetime

from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge
from app.assistant.embeddings.embedder import embed_text
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)
from app.models.base import get_session
from app.assistant.utils.pydantic_classes import Message

class KnowledgeGraphUtils:
    """
    Utility class for knowledge graph operations.
    
    WARNING: This class holds a database session for its lifetime.
    For long-running operations with LLM calls, you should:
    1. Create the instance
    2. Do DB operations quickly
    3. Call close_session() before LLM calls
    4. Create a new instance after LLM calls if more DB ops needed
    
    Better pattern: Use session_factory and get fresh sessions per operation.
    """
    
    def __init__(self, session: Optional[Session] = None, session_factory=None):
        """
        Initialize KnowledgeGraphUtils.
        
        Args:
            session: Existing session to use (will NOT be auto-closed)
            session_factory: Factory to create new sessions (preferred for long-running ops)
        """
        self._owns_session = session is None  # Only close sessions we create
        self._session_factory = session_factory or get_session
        self.session = session or self._session_factory()

    def close_session(self):
        """
        Close the session if we own it.
        Call this before doing slow operations (LLM calls, API calls).
        """
        if self._owns_session and self.session:
            try:
                self.session.close()
            except Exception as e:
                logger.debug(f"KnowledgeGraphUtils: session.close() failed: {e}", exc_info=True)
            self.session = None
    
    def get_fresh_session(self) -> Session:
        """
        Get a fresh session for a new operation.
        Closes the old session if we own it.
        """
        self.close_session()
        self.session = self._session_factory()
        self._owns_session = True
        return self.session
    
    def __enter__(self):
        """Context manager support."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Close session on exit."""
        self.close_session()
        return False

    def create_embedding(self, text: str) -> List[float]:
        """Create semantic embedding for text."""
        return embed_text(text)

    def cosine_similarity(self, vec1, vec2) -> float:
        """Calculate cosine similarity between two vectors."""
        # Handle None or empty cases
        if vec1 is None or vec2 is None:
            return 0.0
        
        # Convert to numpy arrays if they aren't already
        vec1 = np.array(vec1)
        vec2 = np.array(vec2)
        
        # Check if arrays are empty
        if vec1.size == 0 or vec2.size == 0:
            return 0.0
            
        norm_product = np.linalg.norm(vec1) * np.linalg.norm(vec2)
        if norm_product == 0:
            return 0.0
        return np.dot(vec1, vec2) / norm_product

    # ──────────────────  NODE HELPERS  ───────────────────────────────────────────

    def find_fuzzy_match_node(self,
                              label: str,
                              node_type_value: Optional[str] = None,
                              similarity_threshold: float = 0.8,
                              max_results: int = 5) -> List[Tuple[Node, float]]:
        """
        Finds nodes with semantically similar labels, optionally filtered by type.
        Uses ChromaDB for fast vector similarity search.
        """
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        
        # Generate embedding for query
        query_embedding = self.create_embedding(label)
        
        # Use ChromaDB for fast similarity search
        chroma = get_chroma_manager()
        similar_node_ids = chroma.search_similar_nodes(
            query_embedding,
            k=max_results * 3,  # Get more candidates for filtering
            threshold=similarity_threshold
        )
        
        # Fetch actual nodes and filter by type if needed
        results = []
        for node_id, similarity, _ in similar_node_ids:
            node = self.session.query(Node).filter(Node.id == node_id).first()
            if node:
                # Filter by node type if specified
                if node_type_value is None or node.node_type == node_type_value:
                    results.append((node, similarity))
                    if len(results) >= max_results:
                        break
        
        return results


    def create_node(self,
                  node_type: str,
                  label: str,
                  aliases: Optional[List[str]] = None,
                  description: Optional[str] = None,
                  attributes: Optional[Dict[str, Any]] = None,
                  category: Optional[str] = None,
                  # New promoted fields
                  valid_during: Optional[str] = None,
                  hash_tags: Optional[List[str]] = None,
                  start_date: Optional[str] = None,
                  end_date: Optional[str] = None,
                  start_date_confidence: Optional[str] = None,
                  end_date_confidence: Optional[str] = None,
                  semantic_label: Optional[str] = None,
                  goal_status: Optional[str] = None,
                  # Optional metadata fields
                  confidence: Optional[float] = None,
                  importance: Optional[float] = None,
                  source: Optional[str] = None,
                  # New schema fields (provenance)
                  original_message_id: Optional[str] = None,
                  original_sentence: Optional[str] = None,
                  sentence_id: Optional[str] = None
                  ) -> Tuple[Node, str]:
        """
        Creates and adds a new node to the session.

        This function creates a new node without checking for duplicates.
        """
        # Create the new Node object with all provided details.
        node_id = str(uuid.uuid4())  # SQLite requires UUID as string
        new_node = Node(
            id=node_id,
            node_type=node_type,
            label=label,
            category=category,
            aliases=aliases or [], # Default to an empty list if None
            description=description,
            attributes=attributes or {},
            # Note: label_embedding is now a @property that reads from ChromaDB
            # We store the embedding in ChromaDB after creating the node
            # New promoted fields
            valid_during=valid_during,
            hash_tags=hash_tags or [],
            start_date=start_date,
            end_date=end_date,
            start_date_confidence=start_date_confidence,
            end_date_confidence=end_date_confidence,
            semantic_label=semantic_label,
            goal_status=goal_status,
            # New first-class fields
            confidence=confidence,
            importance=importance,
            source=source,
            # New schema fields (provenance - immutable)
            original_message_id=original_message_id,
            original_sentence=original_sentence,
            sentence_id=sentence_id
        )
        
        # Store label embedding in ChromaDB
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        chroma = get_chroma_manager()
        label_embedding = self.create_embedding(label)
        chroma.store_node_embedding(str(node_id), label, label_embedding)

        # Add the new node to the session and commit immediately
        # SQLite single-writer: commit after each write to avoid lock contention
        self.session.add(new_node)
        self.session.commit()

        return new_node, "created"

    # ──────────────────  RELATIONSHIP HELPERS  ───────────────────────────────────

    def find_exact_match_relationship(self,
                                      source_id,  # SQLite: string UUID
                                      target_id,  # SQLite: string UUID
                                      relationship_type: str
                                      ) -> Optional[Edge]:
        """
        Finds an edge by its unique composite key.
        """
        # Ensure IDs are strings for SQLite
        return self.session.query(Edge).filter_by(
            source_id=str(source_id),
            target_id=str(target_id),
            relationship_type=relationship_type
        ).first()


    def safe_add_relationship_by_id(self,
                                    source_id,  # SQLite: string UUID
                                    target_id,  # SQLite: string UUID
                                    relationship_type: str,
                                    attributes: Optional[Dict[str, Any]] = None,
                                    sentence: Optional[str] = None,
                                    original_message_timestamp: Optional[datetime] = None,
                                    confidence: Optional[float] = None,
                                    importance: Optional[float] = None,
                                    source: Optional[str] = None,
                                    # New schema fields
                                    original_message_id: Optional[str] = None,
                                    sentence_id: Optional[str] = None,
                                    relationship_descriptor: Optional[str] = None,
                                    # Semantic deduplication
                                    semantic_dedup: bool = True,
                                    similarity_threshold: float = 0.6
                                    ) -> Tuple[Edge, str]:
        """
        Inserts a relationship using node IDs if it doesn't already exist.
        
        Deduplication strategy:
        1. Exact match: (source_id, target_id, relationship_type)
        2. Semantic match (if enabled): Same nodes + similar sentence (cosine > threshold)
        
        Args:
            semantic_dedup: Enable semantic sentence similarity checking
            similarity_threshold: Cosine similarity threshold (default 0.6)
        """
        attributes = attributes or {}

        # 1. Note: Temporal qualifiers are now stored in attributes only.
        #    Edge uniqueness is based on source, target, and relationship_type only.

        # 2. Check for an existing edge using the composite key (exact match).
        existing_edge = self.find_exact_match_relationship(
            source_id, target_id, relationship_type
        )

        if existing_edge:
            # Edge already exists (exact match), return it
            return existing_edge, "found_exact"
        
        # 3. Semantic deduplication: Check if a semantically similar edge exists
        if semantic_dedup and sentence:
            # Find all edges between these two nodes (any relationship type)
            # Note: sentence_embedding is a @property (ChromaDB), not a column, so we filter in Python
            all_edges = self.session.query(Edge).filter(
                Edge.source_id == source_id,
                Edge.target_id == target_id,
            ).all()
            
            # Filter to edges that have sentences (which means they can have embeddings)
            candidate_edges = [e for e in all_edges if e.sentence]
            
            if candidate_edges:
                # Create embedding for new sentence
                new_sentence_embedding = self.create_embedding(sentence)
                
                # Check similarity with each existing edge
                for existing_edge in candidate_edges:
                    try:
                        # Compute cosine similarity
                        similarity = self.cosine_similarity(
                            new_sentence_embedding,
                            existing_edge.sentence_embedding
                        )
                        
                        if similarity >= similarity_threshold:
                            logger.debug(
                                f"Semantic edge duplicate detected! Similarity: {similarity:.3f} (threshold: {similarity_threshold})"
                                f" | Existing: [{existing_edge.relationship_type}] '{existing_edge.sentence[:100]}...'"
                                f" | New: [{relationship_type}] '{sentence[:100]}...'"
                            )
                            return existing_edge, "found_semantic"
                    except Exception as e:
                        logger.warning(f"Error computing edge similarity: {e}")
                        continue

        # 4. No duplicate found - create a semantically rich embedding for the relationship.
        #    We fetch the node labels to create a sentence representing the triplet.
        # SQLite: Ensure IDs are strings
        source_node = self.session.get(Node, str(source_id))
        target_node = self.session.get(Node, str(target_id))

        if not source_node or not target_node:
            logger.warning(f"Could not create edge because node IDs were not found in DB.")
            return None, "error_missing_nodes"

        # 5. Create and persist the new edge.
        edge_id = str(uuid.uuid4())
        new_edge = Edge(
            id=edge_id,  # SQLite: string UUID
            source_id=str(source_id),  # SQLite: string UUID
            target_id=str(target_id),  # SQLite: string UUID
            relationship_type=relationship_type,
            attributes=attributes,
            sentence=sentence,
            original_message_timestamp=original_message_timestamp,
            # Note: embeddings are now stored in ChromaDB, not as columns
            # New first-class fields
            confidence=confidence,
            importance=importance,
            source=source,
            # New schema fields
            original_message_id=original_message_id,
            sentence_id=sentence_id,
            relationship_descriptor=relationship_descriptor,
        )
        self.session.add(new_edge)
        
        # Store edge embedding in ChromaDB (using sentence if available)
        if sentence:
            from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
            chroma = get_chroma_manager()
            edge_embedding = self.create_embedding(sentence)
            chroma.store_edge_embedding(edge_id, sentence, edge_embedding)
        
        # Commit immediately - SQLite single-writer needs frequent commits
        self.session.commit()
        return new_edge, "created"


    # ──────────────────  TYPE HELPERS  ───────────────────────
    # Valid node types: Entity, Event, State, Goal, Concept, Property

    def get_node_by_id(self, node_id: uuid.UUID) -> Optional[Node]:
        """
        Retrieves a single node by its primary key (ID).

        Args:
            node_id: The UUID of the node to retrieve.

        Returns:
            The Node object if found, otherwise None.
        """
        # Convert UUID to string for SQLite compatibility
        return self.session.get(Node, str(node_id))
    
    def get_node_edges(self, node_id: uuid.UUID) -> List[Edge]:
        """
        Get all edges connected to a node (both as source and target)
        """
        return self.session.query(Edge).filter(
            (Edge.source_id == node_id) | (Edge.target_id == node_id)
        ).all()


    def update_node(self, node_id: uuid.UUID, updates: Dict[str, Any]) -> Optional[Node]:
        """
        Updates an existing node with new data.

        Args:
            node_id: The UUID of the node to update.
            updates: A dictionary with keys like 'aliases', 'description',
                     or 'attributes' to be updated.

        Returns:
            The updated Node object if found, otherwise None.
        """
        # Use the helper we just defined to fetch the node
        node = self.get_node_by_id(node_id)

        if not node:
            logger.warning(f"Could not find node with ID {node_id} to update.")
            return None

        # Update label if provided
        if "label" in updates:
            node.label = updates["label"]

        # Update semantic_label if provided
        if "semantic_label" in updates:
            node.semantic_label = updates["semantic_label"]

        # Update description if provided
        if "description" in updates:
            node.description = updates["description"]

        # Add new aliases, ensuring no duplicates
        if "aliases" in updates and updates["aliases"]:
            # Use a set for efficient de-duplication
            existing_aliases = set(node.aliases or [])
            new_aliases = set(updates["aliases"])

            # Union the sets and convert back to a sorted list for canonical storage
            node.aliases = sorted(list(existing_aliases.union(new_aliases)))

        # Update any other generic attributes
        if "attributes" in updates:
            node.attributes = {**(node.attributes or {}), **updates["attributes"]}

        # Update temporal fields if provided
        if "start_date" in updates:
            node.start_date = updates["start_date"]
        if "end_date" in updates:
            node.end_date = updates["end_date"]
        if "start_date_confidence" in updates:
            node.start_date_confidence = updates["start_date_confidence"]
        if "end_date_confidence" in updates:
            node.end_date_confidence = updates["end_date_confidence"]
        if "valid_during" in updates:
            node.valid_during = updates["valid_during"]

        # The session automatically tracks changes to the 'node' object.
        # The main pipeline is responsible for the final commit.
        return node

    def intelligent_merge_nodes(self, target_node: Node, source_node: Node, node_data_merger) -> Node:
        """
        Intelligently merge two nodes using the node_data_merger agent.
        This is the unified merge function used by both KG pipeline and duplicate merger.
        
        Args:
            target_node: The node to merge into (will be updated)
            source_node: The node to merge from (will be deleted)
            node_data_merger: The node data merger agent instance
            
        Returns:
            The updated target node
        """

        # Prepare existing node data for the agent
        existing_node_data = {
            "node_type": target_node.node_type,
            "label": target_node.label,
            "aliases": target_node.aliases or [],
            "hash_tags": target_node.hash_tags or [],
            "semantic_label": target_node.semantic_label,
            "goal_status": target_node.goal_status,
            "valid_during": target_node.valid_during,
            "category": target_node.category,
            "start_date": target_node.start_date.isoformat() if target_node.start_date and hasattr(target_node.start_date, 'isoformat') else target_node.start_date,
            "end_date": target_node.end_date.isoformat() if target_node.end_date and hasattr(target_node.end_date, 'isoformat') else target_node.end_date,
            "start_date_confidence": target_node.start_date_confidence,
            "end_date_confidence": target_node.end_date_confidence,
            "confidence": target_node.confidence,
            "importance": target_node.importance,
        }
        
        # Prepare new node data for the agent
        new_node_data = {
            "node_type": source_node.node_type,
            "label": source_node.label,
            "aliases": source_node.aliases or [],
            "hash_tags": source_node.hash_tags or [],
            "semantic_label": source_node.semantic_label,
            "goal_status": source_node.goal_status,
            "valid_during": source_node.valid_during,
            "category": source_node.category,
            "start_date": source_node.start_date.isoformat() if source_node.start_date and hasattr(source_node.start_date, 'isoformat') else source_node.start_date,
            "end_date": source_node.end_date.isoformat() if source_node.end_date and hasattr(source_node.end_date, 'isoformat') else source_node.end_date,
            "start_date_confidence": source_node.start_date_confidence,
            "end_date_confidence": source_node.end_date_confidence,
            "confidence": source_node.confidence,
            "importance": source_node.importance,
        }
        
        # Call the node data merger agent
        merger_input = {
            "existing_node_data": json.dumps(existing_node_data),
            "new_node_data": json.dumps(new_node_data)
        }

        # Release any DB locks held by self.session before the LLM call —
        # node_data_merger.action_handler can take 10–30s, and holding a
        # SQLite write transaction that long cascades "database is locked"
        # errors across other writers. See feedback_no_db_lock_over_llm.
        try:
            self.session.commit()
        except Exception:
            logger.debug("intelligent_merge_nodes pre-LLM commit failed", exc_info=True)
            self.session.rollback()

        merger_response = node_data_merger.action_handler(Message(agent_input=merger_input))
        merger_result = merger_response.data or {}
        
        # Snapshot the target's pre-merge state so the merge log can restore
        # its original fields during a future unmerge. Must happen BEFORE we
        # apply the merger result to the target.
        from app.assistant.kg_core.kg_utils.node_merge import (
            merge_nodes_in_session,
            snapshot_node,
        )
        target_pre_snapshot = snapshot_node(target_node)

        # Apply the merged data to the target node
        self._apply_merged_data_to_node(target_node, merger_result)

        # COMMIT NOW - don't hold dirty session during edge queries
        # This prevents "database is locked" errors from autoflush during queries
        self.session.commit()

        # Reassign edges from source to target (only if source node is persisted)
        # Use SQLAlchemy inspect to check if node is actually in the database
        from sqlalchemy import inspect as sa_inspect
        source_state = sa_inspect(source_node, raiseerr=False)
        is_persisted = source_state is not None and source_state.persistent

        if is_persisted:
            # Route through the central helper so dependents get rebound and a
            # kg_merge_log row is written. Helper handles edge reroute + drop
            # snapshots + registry-based dependent rebind + loser delete.
            merge_nodes_in_session(
                self.session,
                loser_node=source_node,
                winner_node=target_node,
                merge_actor="KnowledgeGraphUtils.intelligent_merge_nodes",
                winner_pre_snapshot=target_pre_snapshot,
            )
            self.session.commit()

        return target_node

    def _normalize_date_value(self, dt_val):
        """
        Normalize date values from merger agent output.
        Accept None, empty, 'unknown', 'null', ':null', ':null,', 'none', or ISO strings.
        Return datetime or None.
        """
        from datetime import datetime
        
        if dt_val is None:
            return None
        if isinstance(dt_val, datetime):
            return dt_val
        if isinstance(dt_val, str):
            # Strip whitespace and convert to lowercase
            s = dt_val.strip().lower()
            # Handle various null representations including those with colons, slashes, and commas
            if s in {"", "unknown", "null", "none", ":null", ":null,", "/null", "_null", "/none", "_none"} or s.startswith(":null") or s.startswith("/null"):
                return None
            try:
                # Handle trailing 'Z'
                if s.endswith("z"):
                    s = s[:-1] + "+00:00"
                return datetime.fromisoformat(s)
            except Exception:
                logger.warning(f"Could not parse date value: {dt_val}, setting to None")
                return None
        return None
    
    def _normalize_confidence_value(self, conf_val):
        """
        Normalize confidence values from merger agent output.
        Accept None, empty, 'unknown', 'null', ':null', ':null,', 'none', or numeric values.
        Return float or None.
        """
        if conf_val is None:
            return None
        if isinstance(conf_val, (int, float)):
            return float(conf_val)
        if isinstance(conf_val, str):
            # Strip whitespace and convert to lowercase
            s = conf_val.strip().lower()
            # Handle various null representations including those with colons, slashes, and commas
            if s in {"", "unknown", "null", "none", ":null", ":null,", "/null", "_null", "/none", "_none"} or s.startswith(":null") or s.startswith("/null"):
                return None
            try:
                return float(s)
            except Exception:
                logger.warning(f"Could not parse confidence value: {conf_val}, setting to None")
                return None
        return None

    def _apply_merged_data_to_node(self, node: Node, merged_data: Dict) -> None:
        """Apply merged data from the node_data_merger agent to a node."""
        
        # Update aliases
        if merged_data.get("merged_aliases"):
            node.aliases = merged_data["merged_aliases"]
        
        # Update hash tags
        if merged_data.get("merged_hash_tags"):
            node.hash_tags = merged_data["merged_hash_tags"]
        
        # Update semantic type
        if merged_data.get("unified_semantic_label"):
            node.semantic_label = merged_data["unified_semantic_label"]
        
        # Update goal status
        if merged_data.get("unified_goal_status"):
            node.goal_status = merged_data["unified_goal_status"]
        
        # Update valid during
        if merged_data.get("unified_valid_during"):
            node.valid_during = merged_data["unified_valid_during"]
        
        # Update category
        if merged_data.get("unified_category"):
            node.category = merged_data["unified_category"]
        
        # Update dates with normalization
        if "unified_start_date" in merged_data:
            node.start_date = self._normalize_date_value(merged_data["unified_start_date"])
        if "unified_end_date" in merged_data:
            node.end_date = self._normalize_date_value(merged_data["unified_end_date"])
        
        # Update date confidence with normalization
        if "unified_start_date_confidence" in merged_data:
            node.start_date_confidence = self._normalize_confidence_value(merged_data["unified_start_date_confidence"])
        if "unified_end_date_confidence" in merged_data:
            node.end_date_confidence = self._normalize_confidence_value(merged_data["unified_end_date_confidence"])
        
        # Update confidence and importance
        if merged_data.get("unified_confidence") is not None:
            node.confidence = merged_data["unified_confidence"]
        if merged_data.get("unified_importance") is not None:
            node.importance = merged_data["unified_importance"]

