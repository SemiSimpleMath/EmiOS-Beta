"""
Shared embedding configuration contract for KG and RAG.

All local embedding producers/consumers should import from here so model
selection cannot silently drift between subsystems.
"""

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_PROVIDER = "onnx"
EMBEDDING_SCHEMA_ID = f"{EMBEDDING_PROVIDER}:{EMBEDDING_MODEL_NAME}"

# Accept the old schema ID from before the PyTorch -> ONNX migration so
# existing ChromaDB collections are not rejected on startup.
LEGACY_EMBEDDING_SCHEMA_ID = f"sentence-transformers:{EMBEDDING_MODEL_NAME}"

