"""
ChromaDB Embedding Manager

Centralized manager for storing and retrieving embeddings in ChromaDB.
This replaces the embedding columns that were removed from SQLite models.

Collections:
- node_embeddings: Node label embeddings (keyed by node.id)
- edge_embeddings: Edge sentence embeddings (keyed by edge.id)
- taxonomy_embeddings: Taxonomy label embeddings (keyed by taxonomy.id)
"""

import os
import threading
from typing import List, Optional, Dict, Tuple
import numpy as np
import chromadb
from chromadb.config import Settings
from app.assistant.embeddings.config import EMBEDDING_MODEL_NAME, EMBEDDING_SCHEMA_ID, LEGACY_EMBEDDING_SCHEMA_ID
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_chroma_kg_db_dir

logger = get_logger(__name__)


def _make_chroma_client(path: str):
    """Create a PersistentClient with telemetry disabled and reset allowed."""
    return chromadb.PersistentClient(
        path=path,
        settings=Settings(anonymized_telemetry=False, allow_reset=True),
    )


class ChromaEmbeddingManager:
    """Manages embeddings in ChromaDB for KG nodes, edges, and taxonomy"""

    _instance = None
    _client = None
    # Class-level lock to serialize first-time __new__/__init__ races. Without
    # this, two threads calling ChromaEmbeddingManager() concurrently during
    # startup could both instantiate and both call _init_collections, doubling
    # up on ChromaDB client creation.
    _init_lock = threading.Lock()

    def __new__(cls):
        """Singleton pattern to ensure one ChromaDB client"""
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super(ChromaEmbeddingManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize ChromaDB client and collections"""
        if self._client is None:
            with self._init_lock:
                if self._client is None:
                    # Chroma has ONE writer: the server process. A test process
                    # opening the real PersistentClient both pollutes the prod
                    # index (test-node ghosts) and can access-violate the rust
                    # client while the server writes concurrently — an
                    # uncatchable native crash that kills the whole pytest run.
                    # Tests must monkeypatch get_chroma_manager with a fake.
                    if os.environ.get("USE_TEST_DB", "").strip().lower() in ("1", "true", "yes"):
                        raise RuntimeError(
                            "ChromaEmbeddingManager refuses to open the real chroma "
                            "index under USE_TEST_DB — monkeypatch "
                            "chroma_embedding_manager.get_chroma_manager with a fake."
                        )
                    chroma_path = get_chroma_kg_db_dir()
                    chroma_path.mkdir(exist_ok=True)
                    # Cross-process guard: only ONE process may open this index
                    # (concurrent multi-process access corrupts the hnsw segments).
                    # Raises ChromaWriterLockError if another process holds it.
                    from app.assistant.kg.chroma.chroma_lock import acquire_chroma_writer_lock
                    acquire_chroma_writer_lock(chroma_path)
                    self._client = _make_chroma_client(str(chroma_path))
                    logger.info("ChromaDB initialized at %s", chroma_path)

                    # Initialize collections
                    self._init_collections()

    @staticmethod
    def _norm_id(raw_id) -> str:
        return str(raw_id).strip()

    @staticmethod
    def _extract_first_embedding(embs) -> Optional[List[float]]:
        """
        Normalize Chroma embedding payload shapes.

        Chroma can return embeddings as:
        - python list (often nested)
        - numpy ndarray with shape (1, dim) or (dim,)
        """
        if embs is None:
            return None
        try:
            arr = np.asarray(embs)
        except Exception as _e:
            logger.error("_extract_first_embedding: could not coerce embedding to numpy array: %s", _e, exc_info=True)
            arr = None
        if arr is not None:
            if arr.size == 0:
                return None
            if arr.ndim >= 2:
                first = arr[0]
            else:
                first = arr
            try:
                if np.asarray(first).size == 0:
                    return None
            except Exception as _e:
                logger.error("_extract_first_embedding: inner numpy size check failed: %s", _e, exc_info=True)
                return None
            return first.tolist() if hasattr(first, "tolist") else list(first)

        if isinstance(embs, list) and len(embs) > 0:
            first = embs[0]
            if first is None:
                return None
            if hasattr(first, "tolist"):
                return first.tolist()
            if isinstance(first, list):
                return first
            return list(first)
        return None
    
    def _init_collections(self):
        """Initialize or get existing collections"""
        try:
            # Node embeddings
            self.node_collection = self._client.get_or_create_collection(
                name="node_embeddings",
                metadata={
                    "description": "Node label embeddings",
                    "embedding_model": EMBEDDING_MODEL_NAME,
                    "embedding_schema": EMBEDDING_SCHEMA_ID,
                },
            )
            
            # Edge embeddings
            self.edge_collection = self._client.get_or_create_collection(
                name="edge_embeddings",
                metadata={
                    "description": "Edge sentence embeddings",
                    "embedding_model": EMBEDDING_MODEL_NAME,
                    "embedding_schema": EMBEDDING_SCHEMA_ID,
                },
            )
            
            # Taxonomy embeddings
            self.taxonomy_collection = self._client.get_or_create_collection(
                name="taxonomy_embeddings",
                metadata={
                    "description": "Taxonomy label embeddings",
                    "embedding_model": EMBEDDING_MODEL_NAME,
                    "embedding_schema": EMBEDDING_SCHEMA_ID,
                },
            )

            # Node context embeddings — richer signal than label-only.
            # Embeds original_sentence + edge sentences for duplicate detection.
            self.node_context_collection = self._client.get_or_create_collection(
                name="node_context_embeddings",
                metadata={
                    "description": "Node context embeddings (sentence + edges)",
                    "embedding_model": EMBEDDING_MODEL_NAME,
                    "embedding_schema": EMBEDDING_SCHEMA_ID,
                },
            )

            # Identity sentence embeddings — the definite-description
            # projection (kg_core/kg_utils/identity_sentence.py). Sentence-vs-
            # sentence similarity is what MiniLM is trained for; this is the
            # primary resolution signal once populated.
            self.node_identity_collection = self._client.get_or_create_collection(
                name="node_identity_embeddings",
                metadata={
                    "description": "Node identity sentence embeddings (definite descriptions)",
                    "embedding_model": EMBEDDING_MODEL_NAME,
                    "embedding_schema": EMBEDDING_SCHEMA_ID,
                },
            )

            self._validate_embedding_contract(self.node_collection, "node_embeddings")
            self._validate_embedding_contract(self.edge_collection, "edge_embeddings")
            self._validate_embedding_contract(self.taxonomy_collection, "taxonomy_embeddings")
            self._validate_embedding_contract(self.node_context_collection, "node_context_embeddings")
            self._validate_embedding_contract(self.node_identity_collection, "node_identity_embeddings")

            logger.info(f"ChromaDB collections initialized:")
            logger.info(f"  - node_embeddings: {self.node_collection.count()} items")
            logger.info(f"  - edge_embeddings: {self.edge_collection.count()} items")
            logger.info(f"  - taxonomy_embeddings: {self.taxonomy_collection.count()} items")
            logger.info(f"  - node_context_embeddings: {self.node_context_collection.count()} items")
            logger.info(f"  - node_identity_embeddings: {self.node_identity_collection.count()} items")
            
        except Exception as e:
            logger.error(f"Error initializing ChromaDB collections: {e}")
            raise

    def _validate_embedding_contract(self, collection, collection_name: str) -> None:
        """
        Verify collection metadata matches the global embedding contract.

        Existing collections may have missing metadata from older builds; that is
        logged as a warning. A conflicting explicit schema/model is treated as an
        error because cross-schema vector mixing is unsafe.
        """
        metadata = collection.metadata or {}
        existing_model = metadata.get("embedding_model")
        existing_schema = metadata.get("embedding_schema")
        if existing_model is None and existing_schema is None:
            logger.warning(
                "Chroma collection '%s' missing embedding metadata; expected model=%s schema=%s",
                collection_name,
                EMBEDDING_MODEL_NAME,
                EMBEDDING_SCHEMA_ID,
            )
            return
        if existing_model and existing_model != EMBEDDING_MODEL_NAME:
            raise RuntimeError(
                f"Embedding model mismatch for collection '{collection_name}': "
                f"expected '{EMBEDDING_MODEL_NAME}', found '{existing_model}'"
            )
        if existing_schema and existing_schema not in (EMBEDDING_SCHEMA_ID, LEGACY_EMBEDDING_SCHEMA_ID):
            raise RuntimeError(
                f"Embedding schema mismatch for collection '{collection_name}': "
                f"expected '{EMBEDDING_SCHEMA_ID}', found '{existing_schema}'"
            )
    
    # ==================== NODE EMBEDDINGS ====================
    
    def store_node_embedding(self, node_id: str, label: str, embedding: List[float]):
        """Store a node's label embedding"""
        try:
            node_id = self._norm_id(node_id)
            self.node_collection.upsert(
                ids=[node_id],
                embeddings=[embedding],
                metadatas=[{"label": label}]
            )
            logger.debug(f"Stored embedding for node {node_id}: {label}")
        except Exception as e:
            logger.error(f"Error storing node embedding: {e}")
            raise
    
    def get_node_embedding(self, node_id: str) -> Optional[List[float]]:
        """Get a node's label embedding"""
        try:
            node_id = self._norm_id(node_id)
            result = self.node_collection.get(
                ids=[node_id],
                include=["embeddings"]
            )
            return self._extract_first_embedding(result.get("embeddings"))
        except Exception as e:
            logger.debug(f"Node embedding not found for {node_id}: {e}")
            return None
    
    def search_similar_nodes(
        self, 
        query_embedding: List[float], 
        k: int = 10,
        threshold: float = 0.0
    ) -> List[Tuple[str, float, str]]:
        """
        Search for similar nodes by embedding
        
        Returns:
            List of (node_id, similarity_score, label)
        """
        try:
            results = self.node_collection.query(
                query_embeddings=[query_embedding],
                n_results=k,
                include=["metadatas", "distances"]
            )
            
            if not results['ids'] or len(results['ids']) == 0:
                return []
            
            # Convert distances to similarities (ChromaDB returns L2 distances)
            # similarity = 1 / (1 + distance)
            similar_nodes = []
            for node_id, distance, metadata in zip(
                results['ids'][0],
                results['distances'][0],
                results['metadatas'][0]
            ):
                similarity = 1 / (1 + distance)
                if similarity >= threshold:
                    label = metadata.get('label', '')
                    similar_nodes.append((node_id, similarity, label))
            
            return similar_nodes
        except Exception as e:
            logger.error("Error searching similar nodes: %s", e, exc_info=True)
            raise
    
    def delete_node_embedding(self, node_id: str):
        """Delete a node's embedding"""
        try:
            self.node_collection.delete(ids=[str(node_id)])
            logger.debug(f"Deleted embedding for node {node_id}")
        except Exception as e:
            logger.error("Error deleting node embedding: %s", e, exc_info=True)
    
    # ==================== NODE CONTEXT EMBEDDINGS ====================

    def store_node_context_embedding(self, node_id: str, context_text: str, embedding: List[float]):
        """Store a node's context embedding (original_sentence + edge sentences)."""
        try:
            node_id = self._norm_id(node_id)
            self.node_context_collection.upsert(
                ids=[node_id],
                embeddings=[embedding],
                metadatas=[{"context_preview": context_text[:200]}],
            )
            logger.debug("Stored context embedding for node %s", node_id)
        except Exception as e:
            logger.error("Error storing node context embedding: %s", e)
            raise

    def get_node_context_embedding(self, node_id: str) -> Optional[List[float]]:
        """Get a node's context embedding."""
        try:
            node_id = self._norm_id(node_id)
            result = self.node_context_collection.get(
                ids=[node_id], include=["embeddings"]
            )
            return self._extract_first_embedding(result.get("embeddings"))
        except Exception as e:
            logger.debug("Node context embedding not found for %s: %s", node_id, e)
            return None

    def delete_node_context_embedding(self, node_id: str):
        """Delete a node's context embedding."""
        try:
            self.node_context_collection.delete(ids=[str(node_id)])
            logger.debug("Deleted context embedding for node %s", node_id)
        except Exception as e:
            logger.error("Error deleting node context embedding: %s", e, exc_info=True)

    # ==================== NODE IDENTITY EMBEDDINGS ====================

    def store_node_identity_embedding(self, node_id: str, sentence: str, embedding: List[float]):
        """Store a node's identity-sentence embedding (definite description)."""
        try:
            node_id = self._norm_id(node_id)
            self.node_identity_collection.upsert(
                ids=[node_id],
                embeddings=[embedding],
                metadatas=[{"identity_sentence": sentence[:300]}],
            )
            logger.debug("Stored identity embedding for node %s", node_id)
        except Exception as e:
            logger.error("Error storing node identity embedding: %s", e)
            raise

    def delete_node_identity_embedding(self, node_id: str):
        """Delete a node's identity-sentence embedding."""
        try:
            self.node_identity_collection.delete(ids=[str(node_id)])
            logger.debug("Deleted identity embedding for node %s", node_id)
        except Exception as e:
            logger.error("Error deleting node identity embedding: %s", e, exc_info=True)

    # ==================== EDGE EMBEDDINGS ====================
    
    def store_edge_embedding(self, edge_id: str, sentence: str, embedding: List[float]):
        """Store an edge's sentence embedding"""
        try:
            edge_id = self._norm_id(edge_id)
            self.edge_collection.upsert(
                ids=[edge_id],
                embeddings=[embedding],
                metadatas=[{"sentence": sentence[:500]}]  # Truncate long sentences
            )
            logger.debug(f"Stored embedding for edge {edge_id}")
        except Exception as e:
            logger.error(f"Error storing edge embedding: {e}")
            raise
    
    def get_edge_embedding(self, edge_id: str) -> Optional[List[float]]:
        """Get an edge's sentence embedding"""
        try:
            edge_id = self._norm_id(edge_id)
            result = self.edge_collection.get(
                ids=[edge_id],
                include=["embeddings"]
            )
            return self._extract_first_embedding(result.get("embeddings"))
        except Exception as e:
            logger.debug(f"Edge embedding not found for {edge_id}: {e}")
            return None
    
    def search_similar_edges(
        self,
        query_embedding: List[float],
        k: int = 10,
        threshold: float = 0.0
    ) -> List[Tuple[str, float]]:
        """
        Search for similar edges by embedding
        
        Returns:
            List of (edge_id, similarity_score)
        """
        try:
            results = self.edge_collection.query(
                query_embeddings=[query_embedding],
                n_results=k,
                include=["distances"]
            )
            
            if not results['ids'] or len(results['ids']) == 0:
                return []
            
            similar_edges = []
            for edge_id, distance in zip(results['ids'][0], results['distances'][0]):
                similarity = 1 / (1 + distance)
                if similarity >= threshold:
                    similar_edges.append((edge_id, similarity))
            
            return similar_edges
        except Exception as e:
            logger.error("Error searching similar edges: %s", e, exc_info=True)
            raise
    
    def delete_edge_embedding(self, edge_id: str):
        """Delete an edge's embedding"""
        try:
            self.edge_collection.delete(ids=[str(edge_id)])
            logger.debug(f"Deleted embedding for edge {edge_id}")
        except Exception as e:
            logger.error("Error deleting edge embedding: %s", e, exc_info=True)
    
    # ==================== TAXONOMY EMBEDDINGS ====================
    
    def store_taxonomy_embedding(self, taxonomy_id: int, label: str, embedding: List[float]):
        """Store a taxonomy's label embedding"""
        try:
            taxonomy_id_str = self._norm_id(taxonomy_id)
            self.taxonomy_collection.upsert(
                ids=[taxonomy_id_str],
                embeddings=[embedding],
                metadatas=[{"label": label}]
            )
            logger.debug(f"Stored embedding for taxonomy {taxonomy_id}: {label}")
        except Exception as e:
            logger.error(f"Error storing taxonomy embedding: {e}")
            raise
    
    def get_taxonomy_embedding(self, taxonomy_id: int) -> Optional[List[float]]:
        """Get a taxonomy's label embedding"""
        try:
            taxonomy_id_str = self._norm_id(taxonomy_id)
            result = self.taxonomy_collection.get(
                ids=[taxonomy_id_str],
                include=["embeddings"]
            )
            return self._extract_first_embedding(result.get("embeddings"))
        except Exception as e:
            logger.debug(f"Taxonomy embedding not found for {taxonomy_id}: {e}")
            return None
    
    def search_similar_taxonomy(
        self,
        query_embedding: List[float],
        k: int = 10,
        threshold: float = 0.0
    ) -> List[Tuple[int, float, str]]:
        """
        Search for similar taxonomy types by embedding
        
        Returns:
            List of (taxonomy_id, similarity_score, label)
        """
        try:
            results = self.taxonomy_collection.query(
                query_embeddings=[query_embedding],
                n_results=k,
                include=["metadatas", "distances"]
            )
            
            if not results['ids'] or len(results['ids']) == 0:
                return []
            
            similar_taxonomy = []
            for tax_id, distance, metadata in zip(
                results['ids'][0],
                results['distances'][0],
                results['metadatas'][0]
            ):
                similarity = 1 / (1 + distance)
                if similarity >= threshold:
                    label = metadata.get('label', '')
                    similar_taxonomy.append((int(tax_id), similarity, label))
            
            return similar_taxonomy
        except Exception as e:
            logger.error("Error searching similar taxonomy: %s", e, exc_info=True)
            raise
    
    def delete_taxonomy_embedding(self, taxonomy_id: int):
        """Delete a taxonomy's embedding"""
        try:
            self.taxonomy_collection.delete(ids=[str(taxonomy_id)])
            logger.debug(f"Deleted embedding for taxonomy {taxonomy_id}")
        except Exception as e:
            logger.error("Error deleting taxonomy embedding: %s", e, exc_info=True)
    
    # ==================== UTILITY METHODS ====================
    
    def reset_all(self):
        """Delete all embeddings (use with caution!)"""
        logger.warning("Resetting all ChromaDB collections...")
        self._client.delete_collection("node_embeddings")
        self._client.delete_collection("edge_embeddings")
        self._client.delete_collection("taxonomy_embeddings")
        self._client.delete_collection("node_context_embeddings")
        self._init_collections()
        logger.info("All ChromaDB collections reset")
    
    def get_stats(self) -> Dict[str, int]:
        """Get statistics about stored embeddings"""
        return {
            "nodes": self.node_collection.count(),
            "node_contexts": self.node_context_collection.count(),
            "node_identities": self.node_identity_collection.count(),
            "edges": self.edge_collection.count(),
            "taxonomy": self.taxonomy_collection.count(),
        }


# Global singleton instance
_chroma_manager = None

def get_chroma_manager() -> ChromaEmbeddingManager:
    """Get the global ChromaDB manager instance"""
    global _chroma_manager
    if _chroma_manager is None:
        _chroma_manager = ChromaEmbeddingManager()
    return _chroma_manager

