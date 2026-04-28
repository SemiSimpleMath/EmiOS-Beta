"""
Shared embedding interface backed by chromadb's ONNX all-MiniLM-L6-v2.

All embedding consumers should call embed_text() / embed_texts() from here
so the model is loaded exactly once and the runtime can be swapped in one place.
"""
from __future__ import annotations

import threading
from typing import List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_ef = None
_lock = threading.Lock()


def _get_ef():
    global _ef
    if _ef is None:
        with _lock:
            if _ef is None:
                from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
                _ef = DefaultEmbeddingFunction()
                logger.info("ONNX embedding function loaded (all-MiniLM-L6-v2)")
    return _ef


def embed_text(text: str) -> List[float]:
    """Embed a single string. Returns a 384-dimensional float vector."""
    return _get_ef()([text])[0].tolist()


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a batch of strings. Returns a list of 384-dim float vectors."""
    return [v.tolist() for v in _get_ef()(texts)]
