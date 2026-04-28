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

    # In knowledge_graph_utils.py

    def find_nodes_by_alias(self, alias: str, node_type_value: Optional[str] = None) -> List[Node]:
        """Finds nodes that have an exact match in their aliases list."""
        if not alias:
            return []
        query = self.session.query(Node)

        # .contains([x]) on SQLite JSON columns generates LIKE '%["x"]%' which
        # only matches single-element arrays. Use quoted-string LIKE instead.
        escaped = alias.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f'%"{escaped}"%'
        conditions = [Node.aliases.like(pattern)]

        if node_type_value:
            conditions.append(Node.node_type == node_type_value)

        return query.filter(*conditions).order_by(Node.created_at.desc()).all()

    def find_exact_match_nodes(self, label: str, node_type_value: Optional[str] = None) -> List[Node]:
        """Returns nodes with an exact label match, optionally filtered by type."""
        query = self.session.query(Node).filter(Node.label == label)
        if node_type_value:
            query = query.filter(Node.node_type == node_type_value)
        return query.order_by(Node.created_at.desc()).all()
    
    def find_case_insensitive_exact_match_nodes(self, label: str, node_type_value: Optional[str] = None) -> List[Node]:
        """Returns nodes with a case-insensitive exact label match, optionally filtered by type."""
        from sqlalchemy import func
        query = self.session.query(Node).filter(func.lower(Node.label) == label.lower())
        if node_type_value:
            query = query.filter(Node.node_type == node_type_value)
        return query.order_by(Node.created_at.desc()).all()


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


    def find_merge_candidates(self, label: str, node_type: str = None, semantic_label: str = None, category: str = None, k: int = 5) -> List[Node]:
        """
        Finds candidate nodes for merging using a multi-step search
        by checking labels, semantic_labels, aliases, and semantic similarity.
        Only returns candidates with the same node_type to prevent cross-type merging.
        Returns candidates ranked by composite similarity score.
        """
        logger.debug(f"FIND_MERGE_CANDIDATES: Looking for '{label}' (type: {node_type}, semantic: {semantic_label})")
        candidates = {} # Use a dict to store unique nodes by ID to prevent duplicates
        
        # Compute embeddings for matching
        label_embedding = self.create_embedding(label)
        semantic_label_embedding = self.create_embedding(semantic_label) if semantic_label else None

        # 0. Special case: Well-known unique entities that should have exact matches
        from app.assistant.kg_core.user_identity import get_primary_user_name
        from app.assistant.utils.assistant_name import get_assistant_name
        _user = get_primary_user_name().lower()
        _asst = get_assistant_name().lower()
        well_known_entities = {_user, _asst, f"{_asst}_ai", f"{_asst}_ai_assistant"}
        if label.lower() in well_known_entities:
            logger.debug(f"FIND_MERGE_CANDIDATES: '{label}' is a well-known entity, doing exact match")
            # For well-known entities, only do case-insensitive exact match
            exact_matches = self.find_case_insensitive_exact_match_nodes(label)
            logger.debug(f"FIND_MERGE_CANDIDATES: Found {len(exact_matches)} exact matches: {[n.label + ' (' + n.node_type + ')' for n in exact_matches]}")
            for node in exact_matches:
                # CRITICAL: Only include candidates with matching node_type
                if node_type is None or node.node_type == node_type:
                    logger.debug(f"FIND_MERGE_CANDIDATES: Adding candidate {node.label} (type: {node.node_type})")
                    candidates[node.id] = node
                else:
                    logger.debug(f"FIND_MERGE_CANDIDATES: Skipping {node.label} (type: {node.node_type}) - type mismatch")
            # Rank and return
            ranked = self._rank_candidates(list(candidates.values()), label_embedding, semantic_label_embedding, category)
            logger.debug(f"FIND_MERGE_CANDIDATES: Returning {len(ranked)} ranked candidates")
            return ranked

        # 1. Case-insensitive exact label match - filter by node_type
        for node in self.find_case_insensitive_exact_match_nodes(label):
            if len(candidates) < k * 2:  # Gather more candidates than k for better ranking
                # CRITICAL: Only include candidates with matching node_type
                if node_type is None or node.node_type == node_type:
                    candidates[node.id] = node

        # 2. Exact case-sensitive label match (if we still need more candidates)
        if len(candidates) < k * 2:
            for node in self.find_exact_match_nodes(label):
                if node.id not in candidates:
                    # CRITICAL: Only include candidates with matching node_type
                    if node_type is None or node.node_type == node_type:
                        candidates[node.id] = node

        # 3. Exact alias match (if we still need more candidates)
        if len(candidates) < k * 2:
            for node in self.find_nodes_by_alias(label):
                if node.id not in candidates:
                    # CRITICAL: Only include candidates with matching node_type
                    if node_type is None or node.node_type == node_type:
                        candidates[node.id] = node

        # 4. Fuzzy label match (if we still need more candidates)
        if len(candidates) < k * 2:
            for node, score in self.find_fuzzy_match_node(label):
                if node.id not in candidates:
                    # CRITICAL: Only include candidates with matching node_type
                    if node_type is None or node.node_type == node_type:
                        candidates[node.id] = node

        # 5. Semantic label match (if provided and we still need more candidates)
        if semantic_label and len(candidates) < k * 2:
            # Search for nodes with similar semantic_label
            semantic_matches = self._find_by_semantic_label(semantic_label, node_type)
            for node in semantic_matches:
                if node.id not in candidates:
                    candidates[node.id] = node

        # Rank all candidates by composite score and return top k
        ranked = self._rank_candidates(list(candidates.values()), label_embedding, semantic_label_embedding, category)
        return ranked[:k]
    
    def _find_by_semantic_label(self, semantic_label: str, node_type: str = None, threshold: float = 0.75) -> List[Node]:
        """Find nodes by semantic_label similarity."""
        semantic_embedding = self.create_embedding(semantic_label)
        query = self.session.query(Node)
        
        if node_type:
            query = query.filter(Node.node_type == node_type)
        
        # Filter nodes that have a semantic_label
        query = query.filter(Node.semantic_label.isnot(None))
        
        all_nodes = query.all()
        matches = []
        
        for node in all_nodes:
            if node.semantic_label:
                node_semantic_embedding = self.create_embedding(node.semantic_label)
                similarity = self.cosine_similarity(semantic_embedding, node_semantic_embedding)
                if similarity >= threshold:
                    matches.append(node)
        
        return matches
    
    def _rank_candidates(self, candidates: List[Node], label_embedding, semantic_label_embedding, category: str = None) -> List[Node]:
        """
        Rank candidates by composite score based on:
        - Label similarity (embedding cosine)
        - Semantic_label similarity (embedding cosine)
        - Category match (bonus)
        - Existing node importance (established entities rank higher)
        """
        if not candidates:
            return []
        
        scored_candidates = []
        
        for candidate in candidates:
            score = 0.0
            
            # 1. Label similarity (weight: 40%)
            if candidate.label_embedding is not None and label_embedding is not None:
                label_sim = self.cosine_similarity(label_embedding, candidate.label_embedding)
                score += label_sim * 0.4
            
            # 2. Semantic_label similarity (weight: 30%)
            if semantic_label_embedding is not None and candidate.semantic_label:
                candidate_semantic_embedding = self.create_embedding(candidate.semantic_label)
                semantic_sim = self.cosine_similarity(semantic_label_embedding, candidate_semantic_embedding)
                score += semantic_sim * 0.3
            
            # 3. Category match (weight: 15%)
            if category and candidate.category:
                if category.lower() == candidate.category.lower():
                    score += 0.15
                elif category.lower() in candidate.category.lower() or candidate.category.lower() in category.lower():
                    score += 0.075  # Partial match
            
            # 4. Importance/establishment bonus (weight: 15%)
            # Higher importance = more established entity = prefer merging into it
            if candidate.importance:
                score += candidate.importance * 0.15
            else:
                score += 0.5 * 0.15  # Default importance
            
            scored_candidates.append((candidate, score))
        
        # Sort by score descending
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        
        # Log ranking
        if logger.isEnabledFor(10):  # DEBUG level
            for i, (cand, score) in enumerate(scored_candidates[:5], 1):
                logger.debug(f"  {i}. {cand.label} (type={cand.node_type}, category={cand.category}, importance={cand.importance}) - score: {score:.3f}")
        
        return [cand for cand, score in scored_candidates]

    def find_similar_nodes(self, label: str, node_type: str) -> List[Tuple[Node, float, str]]:
        """
        Prefers exact match on (label, node_type), falls back to fuzzy matching.
        Returns a list of tuples: (node, score, match_type).
        """
        exact_matches = self.find_exact_match_nodes(label, node_type)
        if exact_matches:
            return [(node, 1.0, "exact") for node in exact_matches]

        fuzzy_matches = self.find_fuzzy_match_node(label, node_type)
        return [(node, score, "semantic") for node, score in fuzzy_matches]


    def format_similar_nodes(self, similar_list):
        return [
            (node.label, score, match_type)
            for node, score, match_type in similar_list
        ]


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
        Use add_node() if you want duplicate checking and merging.
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

    def add_node(self,
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
                      # New schema fields (provenance - immutable)
                      original_message_id: Optional[str] = None,
                      original_sentence: Optional[str] = None,
                      sentence_id: Optional[str] = None,
                      # Merge agents
                      merge_agent=None,
                      node_data_merger=None
                      ) -> Tuple[Node, str]:
        """
        Add a node with duplicate checking and intelligent merging.
        
        Checks for duplicates BEFORE creating the node. If duplicate found, 
        merges into existing node. If not, creates new node.
        
        Returns:
            Tuple of (Node, status) where status is "merged" or "created"
        """
        # First, check for potential duplicates (now ranked by composite score)
        merge_candidates = self.find_merge_candidates(label, node_type, semantic_label=semantic_label, category=category, k=5)
        
        if merge_candidates and merge_agent and node_data_merger:
            # Prepare all candidates with their neighborhood context
            from app.assistant.kg_core.kg_utils.kg_tools import short_describe_node
            
            candidates_data = []
            for candidate in merge_candidates:
                # Get neighborhood context for existing node (if it has edges)
                try:
                    neighborhood = short_describe_node(candidate.id, self.session, k_edges=5, include_sentences=True)
                    neighborhood_summary = neighborhood.get("top_edges", [])
                except Exception:
                    neighborhood_summary = []
                
                candidates_data.append({
                    "id": str(candidate.id),
                    "node_type": candidate.node_type,  # Add node_type for merge agent context
                    "label": candidate.label,
                    "description": candidate.description,
                    "original_sentence": candidate.original_sentence,
                    "neighborhood": neighborhood_summary,  # Context from connected edges
                    "aliases": candidate.aliases or [],
                    "hash_tags": candidate.hash_tags or [],
                    "semantic_label": candidate.semantic_label,
                    "goal_status": candidate.goal_status,
                    "valid_during": candidate.valid_during,
                    "category": candidate.category,
                    "start_date": candidate.start_date.isoformat() if candidate.start_date and hasattr(candidate.start_date, 'isoformat') else candidate.start_date,
                    "end_date": candidate.end_date.isoformat() if candidate.end_date and hasattr(candidate.end_date, 'isoformat') else candidate.end_date,
                    "start_date_confidence": candidate.start_date_confidence,
                    "end_date_confidence": candidate.end_date_confidence,
                    "confidence": candidate.confidence,
                    "importance": candidate.importance,
                })
            
            # Present ALL candidates to merger at once (already ranked by composite score)
            merge_input = {
                "new_node_context": json.dumps({
                    "node_type": node_type,  # Add node_type for merge agent context
                    "label": label,
                    "description": description,
                    "original_sentence": original_sentence,
                    "aliases": aliases or [],
                    "hash_tags": hash_tags or [],
                    "semantic_label": semantic_label,
                    "goal_status": goal_status,
                    "valid_during": valid_during,
                    "category": category,
                    "start_date": start_date.isoformat() if start_date and hasattr(start_date, 'isoformat') else start_date,
                    "end_date": end_date.isoformat() if end_date and hasattr(end_date, 'isoformat') else end_date,
                    "start_date_confidence": start_date_confidence,
                    "end_date_confidence": end_date_confidence,
                    "confidence": confidence,
                    "importance": importance,
                }),
                "existing_node_candidates": json.dumps(candidates_data)  # ALL candidates at once
            }
            
            merge_response = merge_agent.action_handler(Message(agent_input=merge_input))
            merge_result = merge_response.data or {}
            
            if merge_result.get("merge_nodes", False):
                # Find the candidate by ID
                merged_node_id = merge_result.get("merged_node_id")
                if not merged_node_id:
                    logger.warning("Merger said to merge but didn't specify merged_node_id, skipping merge")
                else:
                    # Find the candidate node by ID
                    target_candidate = next((c for c in merge_candidates if str(c.id) == merged_node_id), None)
                    if not target_candidate:
                        logger.warning(f"Merger specified node ID {merged_node_id} but it wasn't in candidates, skipping merge")
                    else:
                        logger.info(f"🔄 Merging new node '{label}' into existing node '{target_candidate.label}' (ID: {target_candidate.id})")
                        
                        # Create temporary source node for merging
                        temp_source_node = Node(
                            id=str(uuid.uuid4()),  # Temporary ID (SQLite: string UUID)
                            node_type=node_type,
                            label=label,
                            category=category,
                            aliases=aliases or [],
                            description=description,
                            attributes=attributes or {},
                            valid_during=valid_during,
                            hash_tags=hash_tags or [],
                            start_date=start_date,
                            end_date=end_date,
                            start_date_confidence=start_date_confidence,
                            end_date_confidence=end_date_confidence,
                            semantic_label=semantic_label,
                            goal_status=goal_status,
                            confidence=confidence,
                            importance=importance,
                            source=source,
                            original_message_id=original_message_id,
                            sentence_id=sentence_id
                        )
                        
                        # Merge into existing node
                        self.intelligent_merge_nodes(target_candidate, temp_source_node, node_data_merger)
                        return target_candidate, "merged"
        
        # No merge found, create new node
        return self.create_node(
            node_type=node_type,
            label=label,
            aliases=aliases,
            description=description,
            attributes=attributes,
            category=category,
            valid_during=valid_during,
            hash_tags=hash_tags,
            start_date=start_date,
            end_date=end_date,
            start_date_confidence=start_date_confidence,
            end_date_confidence=end_date_confidence,
            semantic_label=semantic_label,
            goal_status=goal_status,
            confidence=confidence,
            importance=importance,
            source=source,
            original_message_id=original_message_id,
            original_sentence=original_sentence,
            sentence_id=sentence_id
        )

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

    def safe_add_relationship(self,
                              source_label: str,
                              source_type: str,
                              target_label: str,
                              target_type: str,
                              relationship_type: str,
                              attributes: Optional[Dict[str, Any]] = None,
                              start_date: Optional[datetime] = None,
                              end_date:   Optional[datetime] = None,
                              confidence: Optional[float] = None,
                              importance: Optional[float] = None,
                              source: Optional[str] = None
                              ) -> Tuple[Optional[Edge], str]:
        """
        Inserts a relationship if it doesn't already exist.
        The `attributes` dict now holds all metadata and qualifiers for the relationship.
        """
        # Find source and target nodes first
        src = self.find_exact_match_node(source_label, source_type)
        tgt = self.find_exact_match_node(target_label, target_type)

        if not src or not tgt:
            missing = []
            if not src: missing.append(f"source ('{source_label}')")
            if not tgt: missing.append(f"target ('{target_label}')")
            logger.warning(f"Could not create edge because nodes were missing: {', '.join(missing)}")
            return None, "error_missing_nodes"

        # Check for existing edge using the unique constraint fields
        existing_edge = self.find_exact_match_relationship(
            src.id, tgt.id, relationship_type, start_date, end_date
        )
        if existing_edge:
            # TODO: Implement attribute merging logic if needed.
            return existing_edge, "found"

        # Create and persist the new edge
        edge_id = str(uuid.uuid4())
        new_edge = Edge(
            id=edge_id,  # SQLite: string UUID
            source_id=src.id,  # Already a string from DB
            target_id=tgt.id,  # Already a string from DB
            relationship_type=relationship_type,
            start_date=start_date,
            end_date=end_date,
            attributes=attributes or {},
            # Note: embeddings are now stored in ChromaDB, not as columns
            # New first-class fields
            confidence=confidence,
            importance=importance,
            source=source,
        )
        self.session.add(new_edge)
        
        # Store edge embedding in ChromaDB (using relationship_type as text)
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        chroma = get_chroma_manager()
        edge_embedding = self.create_embedding(relationship_type)
        chroma.store_edge_embedding(edge_id, relationship_type, edge_embedding)
        
        # Commit immediately - SQLite single-writer needs frequent commits
        self.session.commit()
        return new_edge, "created"


# In knowledge_graph_utils.py

    def get_node_merge_context(self, node: Node, edge_limit: int = 3) -> Optional[Dict[str, Any]]:
        """
        Retrieves contextual information about an existing node to assist in LLM-based merge resolution.

        Args:
            node: The Node object to generate context for.
            edge_limit: The max number of incoming/outgoing edges to include.
        """
        if not node:
            return None

        # No need to find the node, it was passed in directly.

        # Nodes no longer have sentence context - only first-class fields
        # Sentence context is now stored on edges, not nodes

        def edge_to_summary(edge: Edge, current_node_id) -> Dict[str, Any]:  # SQLite: string UUID
            # This inner function does not need to change
            if edge.target_id == current_node_id:
                direction = "incoming"
                other_node = edge.source_node
            else:
                direction = "outgoing"
                other_node = edge.target_node

            return {
                "direction": direction,
                "relationship_type": edge.relationship_type,
                "related_node_label": other_node.label,
                "related_node_type": other_node.node_type,
                "sentence": edge.sentence or "(no sentence)"
            }

        # Fetch recent incoming and outgoing edges
        # SQLite: node.id is already a string
        incoming_edges = (
            self.session.query(Edge)
                .filter(Edge.target_id == str(node.id))
                .order_by(Edge.created_at.desc())
                .limit(edge_limit)
                .all()
        )

        outgoing_edges = (
            self.session.query(Edge)
                .filter(Edge.source_id == str(node.id))
                .order_by(Edge.created_at.desc())
                .limit(edge_limit)
                .all()
        )

        edge_summaries = [edge_to_summary(e, node.id) for e in incoming_edges + outgoing_edges]

        return {
            "id": str(node.id),
            "label": node.label,
            "type": node.node_type,
            "edges_sample": edge_summaries
        }


    def get_fact_merge_context(self, node: Node, source_sentence: str) -> Dict[str, Any]:
        """
        Get context for fact merging, similar to get_node_merge_context but focused on fact comparison.
        
        Args:
            node: The existing node to get context for
            source_sentence: The new source sentence for comparison
            
        Returns:
            Dictionary with context information for fact comparison
        """
        # Get basic node info
        context = {
            "node_id": str(node.id),
            "label": node.label,
            "type": node.node_type,
            "node_classification": node.node_classification,
            "description": node.description,
            "aliases": node.aliases or [],
            "attributes": node.attributes or {},
            "new_source_sentence": source_sentence,
            "existing_source_sentence": node.sentence or "",
            "created_at": node.created_at.isoformat() if node.created_at else None
        }
        
        # Add classification-specific context
        if node.node_classification == "property_node":
            context.update({
                "property_type": node.attributes.get("property_type"),
                "property_value": node.attributes.get("property_value"),
                "property_units": node.attributes.get("property_units"),
                "entity_name": node.attributes.get("entity_name")
            })
        elif node.node_classification == "event_node":
            context.update({
                "event_type": node.attributes.get("event_type"),
                "event_date": node.attributes.get("event_date"),
                "event_participants": node.attributes.get("event_participants", [])
            })
        elif node.node_classification == "goal_node":
            context.update({
                "goal_type": node.attributes.get("goal_type"),
                "goal_timeframe": node.attributes.get("goal_timeframe"),
                "goal_participants": node.attributes.get("goal_participants", [])
            })
        elif node.node_classification == "state_node":
            context.update({
                "state_type": node.attributes.get("state_type"),
                "state_timeframe": node.attributes.get("state_timeframe"),
                "state_participants": node.attributes.get("state_participants", [])
            })
        
        return context

    def get_edge_merge_context(self, edge: Edge) -> Dict[str, Any]:
        """
        Retrieves contextual information about an existing edge to assist in LLM-based merge resolution.
        
        Args:
            edge: The Edge object to generate context for.
        """
        if not edge:
            return None
        
        return {
            "id": str(edge.id),
            "relationship_type": edge.relationship_type,
            "source_node_label": edge.source_node.label if edge.source_node else "Unknown",
            "target_node_label": edge.target_node.label if edge.target_node else "Unknown",
            "sentence": edge.sentence or "",
            "original_message_id": edge.original_message_id or "",
            "sentence_id": edge.sentence_id or "",
            "relationship_descriptor": edge.relationship_descriptor or "",
            "created_at": edge.created_at.isoformat() if edge.created_at else None
        }

    def find_similar_edges(self, source_id: uuid.UUID, target_id: uuid.UUID, relationship_type: str, k: int = 5) -> List[Edge]:
        """
        Finds candidate edges for merging using a multi-step search.
        
        Args:
            source_id: Source node ID
            target_id: Target node ID  
            relationship_type: Relationship type
            k: Maximum number of candidates to return
            
        Returns:
            List of candidate edges
        """
        candidates = []
        
        # 1. Exact match on source, target, and type
        exact_matches = self.session.query(Edge).filter_by(
            source_id=source_id,
            target_id=target_id,
            relationship_type=relationship_type
        ).limit(k).all()
        candidates.extend(exact_matches)
        
        # 2. If we need more candidates, look for same source/target with different types
        if len(candidates) < k:
            similar_edges = self.session.query(Edge).filter_by(
                source_id=source_id,
                target_id=target_id
            ).filter(Edge.relationship_type != relationship_type).limit(k - len(candidates)).all()
            candidates.extend(similar_edges)
        
        # 3. If we still need more, look for semantic similarity in relationship types
        if len(candidates) < k:
            # This could be enhanced with semantic matching of relationship types
            pass
        
        return candidates[:k]

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

    # ──────────────────  UTILITY  ───────────────────────────────────────────────

    def close(self):
        """Closes the session."""
        if self.session:
            self.session.close()

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
        # errors across other writers. Verified no-op for the two callers
        # (create_node_or_merge, merge_multiple_duplicates): neither has
        # pending writes at this point. See feedback_no_db_lock_over_llm.
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

    def merge_multiple_duplicates(self, node_ids: List[str], node_data_merger) -> Dict:
        """
        Merge multiple duplicate nodes using intelligent sequential merging.
        This is the main function for duplicate merger to use.
        
        Args:
            node_ids: List of node IDs to merge
            node_data_merger: The node data merger agent instance
            
        Returns:
            Dictionary with merge results
        """
        if len(node_ids) < 2:
            return {"status": "skipped", "reason": "Need at least 2 nodes to merge"}
        
        try:
            # Get all nodes
            nodes = []
            for node_id in node_ids:
                node = self.get_node_by_id(node_id)
                if node:
                    nodes.append(node)
            
            if len(nodes) < 2:
                return {"status": "skipped", "reason": "Not enough valid nodes found"}
            
            # Sort nodes by edge count (most connected first) - this will be our target node
            nodes.sort(key=lambda n: len(self.get_node_edges(n.id)), reverse=True)
            target_node = nodes[0]
            nodes_to_merge = nodes[1:]
            
            # Sequentially merge each node into the target using intelligent merging
            merge_results = []
            for node in nodes_to_merge:
                try:
                    # Use the intelligent merge function
                    self.intelligent_merge_nodes(target_node, node, node_data_merger)
                    
                    merge_results.append({
                        "merged_node_id": str(node.id),
                        "merged_node_label": node.label,
                        "target_node_id": str(target_node.id),
                        "target_node_label": target_node.label,
                        "status": "success"
                    })
                    
                except Exception as e:
                    merge_results.append({
                        "merged_node_id": str(node.id),
                        "merged_node_label": node.label,
                        "target_node_id": str(target_node.id),
                        "target_node_label": target_node.label,
                        "status": "failed",
                        "error": str(e)
                    })
            
            # Commit the changes
            self.session.commit()
            
            return {
                "status": "completed",
                "target_node_id": str(target_node.id),
                "target_node_label": target_node.label,
                "merge_results": merge_results,
                "total_merged": len([r for r in merge_results if r["status"] == "success"])
            }
            
        except Exception as e:
            self.session.rollback()
            return {
                "status": "failed",
                "error": str(e),
                "node_ids": node_ids
            }


# todos,  maybe when checking of node exist (exact match), check if there is match to some nodes aliases.