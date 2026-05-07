"""
Taxonomy Manager - Handles database operations for taxonomy classification.
"""
from typing import List, Dict, Optional, Any
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.assistant.kg_core.taxonomy.models import Taxonomy, NodeTaxonomyLink, TaxonomySuggestion
from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc, utc_now
logger = get_logger(__name__)


class TaxonomyManager:
    """Manages taxonomy database operations for classification."""
    
    def __init__(self, session: Session):
        self.session = session
        self.kg_utils = KnowledgeGraphUtils(session)
    
    def semantic_search_taxonomy(
        self,
        text: str,
        k: int = 10,
        parent_filter: Optional[int] = None,
        parent_label: Optional[str] = None
    ) -> List[Dict]:
        """
        Find taxonomy types by semantic similarity to text.
        
        Args:
            text: Search query (label or description)
            k: Number of results to return
            parent_filter: Optional parent_id to restrict search to subtree
            parent_label: Optional parent label (e.g., "entity", "event") to restrict search
            
        Returns:
            List of dicts with id, label, parent_id, similarity
        """
        # Convert parent_label to parent_filter if provided
        if parent_label and not parent_filter:
            parent_node = self.session.query(Taxonomy).filter(Taxonomy.label == parent_label).first()
            if parent_node:
                parent_filter = parent_node.id
            else:
                logger.warning(f"Parent label '{parent_label}' not found, searching all taxonomy")
        
        try:
            # Generate embedding for search text
            query_embedding = self.kg_utils.create_embedding(text)
            
            # Use ChromaDB for fast similarity search
            from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
            chroma = get_chroma_manager()
            
            # Search ChromaDB for similar taxonomy entries
            similar_taxonomy = chroma.search_similar_taxonomy(
                query_embedding,
                k=k * 3,  # Get more candidates for filtering
                threshold=0.0  # No threshold, we'll filter by parent later
            )
            
            # If parent filter specified, filter to subtree
            if parent_filter is not None:
                # Get subtree IDs using recursive query
                from sqlalchemy import text as sql_text
                subtree_query = sql_text("""
                    WITH RECURSIVE subtree AS (
                        SELECT id FROM taxonomy WHERE id = :parent_id
                        UNION ALL
                        SELECT t.id FROM taxonomy t
                        INNER JOIN subtree s ON t.parent_id = s.id
                    )
                    SELECT id FROM subtree
                """)
                result = self.session.execute(subtree_query, {"parent_id": parent_filter})
                subtree_ids = set(row[0] for row in result)
                
                # Filter results to only include subtree
                similar_taxonomy = [
                    (tax_id, sim, label) 
                    for tax_id, sim, label in similar_taxonomy 
                    if tax_id in subtree_ids
                ]
            
            # Take top k results
            top_matches = similar_taxonomy[:k]
            
            # Fetch actual Taxonomy objects
            similarities = []
            for tax_id, sim, label in top_matches:
                tax = self.session.query(Taxonomy).filter(Taxonomy.id == tax_id).first()
                if tax:
                    similarities.append((tax, sim))
            
            # Format results
            candidates = []
            for tax, sim in similarities:
                candidates.append({
                    "id": tax.id,
                    "label": tax.label,
                    "parent_id": tax.parent_id,
                    "description": tax.description,
                    "similarity": float(sim)
                })
            
            return candidates
            
        except Exception as e:
            logger.error(f"Error in semantic_search_taxonomy: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []
    
    def get_taxonomy_by_id(self, taxonomy_id: int) -> Optional[Taxonomy]:
        """Get taxonomy type by ID."""
        return self.session.query(Taxonomy).filter(Taxonomy.id == taxonomy_id).first()
    
    def get_taxonomy_by_label(self, label: str) -> Optional[Taxonomy]:
        """Get taxonomy type by exact label match."""
        return self.session.query(Taxonomy).filter(Taxonomy.label == label).first()
    
    def create_taxonomy_safe(
        self,
        label: str,
        parent_id: Optional[int] = None,
        description: Optional[str] = None,
        check_duplicates: bool = True
    ) -> Dict[str, Any]:
        """
        Safely create a new taxonomy entry with duplicate checking.
        
        Args:
            label: Normalized label for the type
            parent_id: Parent taxonomy ID (None for root)
            description: Optional description
            check_duplicates: Whether to check for duplicates (default: True)
            
        Returns:
            Dict with success status, message, and taxonomy object if successful
        """
        try:
            # Normalize label
            normalized_label = label.strip().lower().replace(' ', '_')
            
            # Check for duplicates if requested
            if check_duplicates:
                existing = self.session.query(Taxonomy).filter(
                    Taxonomy.label == normalized_label,
                    Taxonomy.parent_id == (parent_id if parent_id is not None else None)
                ).first()
                
                if existing:
                    return {
                        "success": False,
                        "message": f"Taxonomy '{normalized_label}' already exists under this parent",
                        "existing_taxonomy": existing
                    }
            
            # Create entry (embedding will be stored in ChromaDB)
            tax = Taxonomy(
                label=normalized_label,
                description=description or f"Created via safe taxonomy creation",
                parent_id=parent_id,
                # Note: label_embedding is now a @property that reads from ChromaDB
            )
            
            self.session.add(tax)
            self.session.commit()  # Commit immediately - SQLite single-writer
            
            # Store embedding in ChromaDB
            from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
            chroma = get_chroma_manager()
            embedding = self.kg_utils.create_embedding(normalized_label)
            chroma.store_taxonomy_embedding(tax.id, normalized_label, embedding)
            
            logger.info(f"Created taxonomy safely: {normalized_label} (id={tax.id})")
            
            return {
                "success": True,
                "message": f"Created taxonomy '{normalized_label}'",
                "taxonomy": tax
            }
            
        except Exception as e:
            logger.error(f"Error creating taxonomy '{label}': {str(e)}")
            return {
                "success": False,
                "message": f"Error creating taxonomy: {str(e)}",
                "taxonomy": None
            }

    def create_taxonomy_placeholder(
        self,
        label: str,
        description: Optional[str] = None,
        parent_id: Optional[int] = None
    ) -> Taxonomy:
        """Create a new taxonomy entry without duplicate-checking.

        Wraps ``create_taxonomy_safe`` for callers that want a placeholder row
        right now and will reconcile duplicates later. Returns the Taxonomy
        directly (not the success-dict envelope).
        """
        result = self.create_taxonomy_safe(
            label=label,
            parent_id=parent_id,
            description=description,
            check_duplicates=False,
        )
        
        if result["success"]:
            return result["taxonomy"]
        else:
            raise Exception(result["message"])

    def create_taxonomy_path_safe(
        self,
        path: str,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Safely create a full taxonomy path with duplicate checking.
        
        Args:
            path: Full taxonomy path (e.g., "entity > person > software_developer")
            description: Optional description for the final node
            
        Returns:
            Dict with success status, message, and final taxonomy ID
        """
        try:
            # Parse path
            path_parts = [p.strip() for p in path.split('>')]
            if not path_parts:
                return {
                    "success": False,
                    "message": "Empty path provided"
                }
            
            # Check if the full path already exists
            existing_id = self.find_taxonomy_by_path(path)
            if existing_id:
                existing_tax = self.session.get(Taxonomy, existing_id)
                return {
                    "success": True,
                    "message": f"Path already exists: {path}",
                    "taxonomy_id": existing_id,
                    "taxonomy": existing_tax,
                    "created": False
                }
            
            # Create path hierarchically
            parent_id = None
            created_parts = []
            
            for i, part in enumerate(path_parts):
                # Check if this part exists under the current parent
                existing = self.session.query(Taxonomy).filter(
                    Taxonomy.label == part,
                    Taxonomy.parent_id == (parent_id if parent_id is not None else None)
                ).first()
                
                if existing:
                    parent_id = existing.id
                    created_parts.append(f"{part} (existing)")
                else:
                    # Create this part
                    result = self.create_taxonomy_safe(
                        label=part,
                        parent_id=parent_id,
                        description=description if i == len(path_parts) - 1 else None,
                        check_duplicates=True
                    )
                    
                    if not result["success"]:
                        return {
                            "success": False,
                            "message": f"Failed to create '{part}': {result['message']}"
                        }
                    
                    parent_id = result["taxonomy"].id
                    created_parts.append(f"{part} (created)")
            
            return {
                "success": True,
                "message": f"Created path: {path}",
                "taxonomy_id": parent_id,
                "created_parts": created_parts,
                "created": True
            }
            
        except Exception as e:
            logger.error(f"Error creating taxonomy path '{path}': {str(e)}")
            return {
                "success": False,
                "message": f"Error creating taxonomy path: {str(e)}"
            }
    
    def link_node_to_taxonomy(
        self,
        node_id: str,
        taxonomy_id: int,
        confidence: float,
        source: str = "classifier"
    ) -> NodeTaxonomyLink:
        """
        Create a link between a node and a taxonomy type, or increment count if it already exists.
        
        **Progressive Classification**: Each time the same classification is made,
        we increment the count and update last_seen. This allows:
        - Emergence of truth (most common classification rises to top)
        - Multi-dimensional tagging (nodes have multiple taxonomies)
        - Temporal tracking (when was this classification last confirmed)
        
        Args:
            node_id: UUID of the node
            taxonomy_id: ID of the taxonomy type
            confidence: Confidence score (0.0-1.0)
            source: Source of the classification (e.g., "classifier", "manual")
            
        Returns:
            NodeTaxonomyLink object (created or updated)
        """
        # Check if link already exists
        existing = self.session.query(NodeTaxonomyLink).filter(
            NodeTaxonomyLink.node_id == node_id,
            NodeTaxonomyLink.taxonomy_id == taxonomy_id
        ).first()
        
        if existing:
            # Increment count and update last_seen
            existing.count += 1
            existing.last_seen = utc_now()
            
            # Update confidence using weighted average (favor recent classifications)
            # Formula: new_conf = 0.7 * old_conf + 0.3 * new_conf (gives 30% weight to new data)
            existing.confidence = 0.7 * existing.confidence + 0.3 * confidence
            
            # Update source if it changed
            if source != existing.source:
                existing.source = f"{existing.source},{source}"  # Track all sources
            
            logger.info(f"🔄 Incremented taxonomy link: node={node_id}, taxonomy={taxonomy_id}, count={existing.count}, conf={existing.confidence:.2f}")
            return existing
        
        # Create new link
        link = NodeTaxonomyLink(
            node_id=node_id,
            taxonomy_id=taxonomy_id,
            confidence=confidence,
            source=source,
            count=1,
            last_seen=utc_now()
        )
        
        self.session.add(link)
        self.session.commit()  # Commit immediately - SQLite single-writer
        
        logger.info(f"✨ Created taxonomy link: node={node_id}, taxonomy={taxonomy_id}, count=1, conf={confidence:.2f}")
        
        return link
    
    def get_node_taxonomies(self, node_id: str, order_by: str = "count") -> List[Dict]:
        """
        Get all taxonomy classifications for a node.
        
        Args:
            node_id: UUID of the node
            order_by: Sort order - "count" (most common first), "confidence" (highest first), or "last_seen" (most recent first)
        
        Returns:
            List of dicts with taxonomy_id, label, confidence, source, count, last_seen
        """
        query = self.session.query(NodeTaxonomyLink, Taxonomy).join(
            Taxonomy, NodeTaxonomyLink.taxonomy_id == Taxonomy.id
        ).filter(NodeTaxonomyLink.node_id == node_id)
        
        # Apply ordering
        if order_by == "count":
            query = query.order_by(NodeTaxonomyLink.count.desc())
        elif order_by == "confidence":
            query = query.order_by(NodeTaxonomyLink.confidence.desc())
        elif order_by == "last_seen":
            query = query.order_by(NodeTaxonomyLink.last_seen.desc())
        
        links = query.all()
        
        return [
            {
                "taxonomy_id": link.taxonomy_id,
                "label": tax.label,
                "path": self.get_taxonomy_path(link.taxonomy_id),
                "confidence": link.confidence,
                "source": link.source,
                "count": link.count,
                "last_seen": link.last_seen.isoformat() if link.last_seen else None
            }
            for link, tax in links
        ]
    
    def get_primary_taxonomy(self, node_id: str) -> Optional[Dict]:
        """
        Get the most common taxonomy classification for a node.
        
        This uses a weighted score:
        - Count (frequency): 60% weight
        - Confidence: 30% weight
        - Recency (days since last_seen): 10% weight
        
        Returns:
            Dict with taxonomy info, or None if no classifications
        """
        taxonomies = self.get_node_taxonomies(node_id, order_by="count")
        if not taxonomies:
            return None
        
        # Calculate weighted score for each taxonomy
        now = utc_now()
        max_count = max(t['count'] for t in taxonomies)
        
        for tax in taxonomies:
            # Normalize count (0-1)
            count_score = tax['count'] / max_count if max_count > 0 else 0
            
            # Confidence already 0-1
            confidence_score = tax['confidence']
            
            # Recency score (1.0 for today, decays over 30 days)
            if tax['last_seen']:
                # parse_iso_utc returns aware UTC matching aware `now` above.
                last_seen = parse_iso_utc(tax['last_seen'])
                days_ago = (now - last_seen).days
                recency_score = max(0, 1.0 - (days_ago / 30.0))
            else:
                recency_score = 0
            
            # Weighted score
            tax['score'] = (
                0.6 * count_score +
                0.3 * confidence_score +
                0.1 * recency_score
            )
        
        # Return highest scoring taxonomy
        return max(taxonomies, key=lambda t: t['score'])
    
    def get_taxonomy_path(self, taxonomy_id: int) -> List[str]:
        """
        Get the full path from root to this taxonomy type.
        
        Returns:
            List of labels from root to this type (e.g., ["entity", "person", "family_member", "father"])
        """
        tax = self.get_taxonomy_by_id(taxonomy_id)
        if not tax:
            return []
        
        path = []
        current = tax
        while current:
            path.insert(0, current.label)
            if current.parent_id:
                current = self.get_taxonomy_by_id(current.parent_id)
            else:
                break
        
        return path
    
    def find_taxonomy_by_path(self, path: str) -> Optional[int]:
        """
        Find a taxonomy node by its full path string.
        
        Args:
            path: Path string like "entity > person > user" or "entity.person.user"
            
        Returns:
            Taxonomy ID if found, None otherwise
        """
        # Normalize path - accept both " > " and "." as separators
        if " > " in path:
            labels = [label.strip() for label in path.split(" > ")]
        elif "." in path:
            labels = [label.strip() for label in path.split(".")]
        else:
            # Single label - find it without parent constraint
            tax = self.session.query(Taxonomy).filter(
                Taxonomy.label == path.strip()
            ).first()
            return tax.id if tax else None
        
        # Walk down the path from root to leaf
        current_parent_id = None
        for label in labels:
            if current_parent_id is None:
                # Root level
                tax = self.session.query(Taxonomy).filter(
                    Taxonomy.label == label,
                    Taxonomy.parent_id.is_(None)
                ).first()
            else:
                # Child level
                tax = self.session.query(Taxonomy).filter(
                    Taxonomy.label == label,
                    Taxonomy.parent_id == current_parent_id
                ).first()
            
            if not tax:
                return None  # Path doesn't exist
            
            current_parent_id = tax.id
        
        return current_parent_id  # Return the final (leaf) taxonomy ID
    
    def save_taxonomy_suggestion(
        self,
        suggested_type: str,
        node_label: str,
        node_sentence: str,
        node_type: str,
        parent_candidate_id: Optional[int],
        match_quality: int
    ):
        """
        Save or update a taxonomy suggestion for Phase 2 processing.
        
        If the same suggestion already exists, increment its count.
        Otherwise, create a new suggestion record.
        
        Args:
            suggested_type: The suggested taxonomy type label
            node_label: Label of the node that triggered this suggestion
            node_sentence: Sentence context
            node_type: Node type (Entity, Event, State, etc.)
            parent_candidate_id: ID of taxonomy type that could be the parent
            match_quality: 1-10 rating from classifier agent
        """
        # Check if this suggestion already exists
        existing = self.session.query(TaxonomySuggestion).filter(
            TaxonomySuggestion.suggested_type == suggested_type
        ).first()
        
        if existing:
            # Increment count
            existing.count += 1
            existing.updated_at = utc_now()
            logger.info(f"📊 Incremented suggestion count for '{suggested_type}': {existing.count} occurrences")
        else:
            # Create new suggestion
            suggestion = TaxonomySuggestion(
                suggested_type=suggested_type,
                node_label=node_label,
                node_sentence=node_sentence,
                node_type=node_type,
                parent_candidate_id=parent_candidate_id,
                match_quality=match_quality,
                count=1
            )
            self.session.add(suggestion)
            logger.info(f"💾 Saved new taxonomy suggestion: '{suggested_type}' (parent candidate: {parent_candidate_id})")
        
        self.session.commit()  # Commit immediately - SQLite single-writer
    
    def get_top_suggestions(self, limit: int = 10) -> List[Dict]:
        """
        Get the most frequently suggested taxonomy types.
        
        Useful for identifying which new types should be prioritized for creation.
        
        Returns:
            List of dicts with suggestion details, ordered by count descending
        """
        suggestions = self.session.query(TaxonomySuggestion).order_by(
            TaxonomySuggestion.count.desc(),
            TaxonomySuggestion.created_at.desc()
        ).limit(limit).all()
        
        results = []
        for sugg in suggestions:
            parent_label = None
            if sugg.parent_candidate_id:
                parent = self.get_taxonomy_by_id(sugg.parent_candidate_id)
                parent_label = parent.label if parent else None
            
            results.append({
                "id": sugg.id,
                "suggested_type": sugg.suggested_type,
                "count": sugg.count,
                "parent_candidate_id": sugg.parent_candidate_id,
                "parent_candidate_label": parent_label,
                "match_quality": sugg.match_quality,
                "example_label": sugg.node_label,
                "example_sentence": sugg.node_sentence,
                "created_at": sugg.created_at
            })
        
        return results

