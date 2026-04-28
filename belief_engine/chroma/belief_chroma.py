"""
ChromaDB collection for belief embeddings.

Uses the same sentence-transformer model and ChromaDB client as the KG,
but in a dedicated collection so it's fully isolated.

Usage:
    from belief_engine.chroma.belief_chroma import get_belief_chroma

    bc = get_belief_chroma()
    bc.upsert(belief_id="...", statement="Jukka takes the dogs out at 8am.")
    matches = bc.search(query="morning dog walk", k=5)
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from app.assistant.embeddings.config import EMBEDDING_MODEL_NAME, EMBEDDING_SCHEMA_ID
from app.assistant.embeddings.embedder import embed_text
from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "belief_engine_beliefs"
_instance: Optional["BeliefChroma"] = None


def _embed(text: str) -> List[float]:
    return embed_text(text)


def get_belief_chroma() -> "BeliefChroma":
    global _instance
    if _instance is None:
        _instance = BeliefChroma()
    return _instance


class BeliefChroma:
    """
    Thin wrapper around a dedicated ChromaDB collection for belief embeddings.
    Reuses the shared ChromaEmbeddingManager client but manages its own embeddings.
    """

    def __init__(self) -> None:
        manager = get_chroma_manager()
        self._collection = manager._client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={
                "description": "Belief engine — belief statement embeddings",
                "embedding_model": EMBEDDING_MODEL_NAME,
                "embedding_schema": EMBEDDING_SCHEMA_ID,
            },
        )
        logger.info(
            "[BeliefChroma] Collection '%s' ready (%d entries).",
            _COLLECTION_NAME,
            self._collection.count(),
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert(self, *, belief_id: str, statement: str, domain: str = "") -> None:
        """Embed statement and upsert into the collection."""
        vector = _embed(statement)
        self._collection.upsert(
            ids=[belief_id],
            embeddings=[vector],
            metadatas=[{"domain": domain, "statement": statement[:500]}],
        )

    def delete(self, belief_id: str) -> None:
        try:
            self._collection.delete(ids=[belief_id])
        except Exception as exc:
            logger.debug("[BeliefChroma] delete noop for id=%s: %s", belief_id, exc)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        k: int = 10,
        domain: Optional[str] = None,
    ) -> List[Tuple[str, float, str]]:
        """
        Semantic search over belief statements.

        Returns list of (belief_id, similarity_score, statement_snippet).
        similarity_score is 0–1 (1 = identical).
        """
        vector = _embed(query)
        where = {"domain": domain} if domain else None
        kwargs = dict(
            query_embeddings=[vector],
            n_results=min(k, max(1, self._collection.count())),
            include=["metadatas", "distances"],
        )
        if where:
            kwargs["where"] = where
        results = self._collection.query(**kwargs)

        out: List[Tuple[str, float, str]] = []
        ids = results.get("ids", [[]])[0]
        distances = results.get("distances", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        for bid, dist, meta in zip(ids, distances, metadatas):
            similarity = 1.0 / (1.0 + dist)
            snippet = (meta or {}).get("statement", "")
            out.append((bid, similarity, snippet))
        return out

    def count(self) -> int:
        return self._collection.count()

    def get_all_for_domain(self, domain: str) -> List[Tuple[str, List[float]]]:
        """
        Retrieve all (belief_id, embedding_vector) pairs for a domain.
        Used by canonicalization to sort beliefs by proximity.
        """
        results = self._collection.get(
            where={"domain": domain},
            include=["embeddings"],
        )
        ids = results.get("ids") or []
        embeddings = results.get("embeddings") or []
        return list(zip(ids, embeddings))
