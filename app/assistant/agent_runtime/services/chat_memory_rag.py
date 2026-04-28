"""Chat memory RAG — semantic search over recent chat summaries.

Provides conversational continuity across sessions by finding past chat
context relevant to the current message.

Two operations:
1. index_recent_summaries() — batch-embed recent chat summaries into ChromaDB
2. recall(query, top_k) — semantic search for relevant past conversations
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_COLLECTION_NAME = "chat_memory"
_SUMMARY_LOOKBACK_DAYS = 90   # Summaries are compact and carry biographical context.
_USER_MSG_LOOKBACK_DAYS = 14  # Raw user messages are noisy — keep recent only.
_DEFAULT_TOP_K = 8


_chroma_client = None


def _get_chroma_client():
    """Get or create a PersistentClient for chat memory."""
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        from chromadb.config import Settings
        from app.assistant.utils.path_utils import get_repo_root

        db_path = str(get_repo_root() / "data" / "chroma_chat_memory")
        _chroma_client = chromadb.PersistentClient(
            path=db_path,
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
    return _chroma_client


def _get_collection():
    """Get or create the chat_memory collection."""
    client = _get_chroma_client()
    return client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def _stable_id(text: str, timestamp: str) -> str:
    """Generate a stable document ID from content + timestamp."""
    seed = f"{text}|{timestamp}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def index_recent_summaries(
    *,
    room_id: str = "master_room",
) -> int:
    """Load recent chat summaries and user messages from DB and embed into ChromaDB.

    Summaries (history_summary) are indexed over a longer window (90 days)
    because they're compact and carry biographical context.
    Raw user messages are indexed over a shorter window (14 days).

    Returns count of documents indexed.
    """
    from sqlalchemy import select
    from app.models.base import get_session
    from app.assistant.database.db_handler import UnifiedLog2026

    # Use the wider window for the query — we'll filter per-type below.
    cutoff = datetime.now(timezone.utc) - timedelta(days=_SUMMARY_LOOKBACK_DAYS)
    session = get_session()
    try:
        rows = session.execute(
            select(UnifiedLog2026)
            .where(UnifiedLog2026.room_id == room_id)
            .where(UnifiedLog2026.timestamp >= cutoff)
            .where(UnifiedLog2026.source.in_(["room_summary::" + room_id, "room_ui"]))
            .order_by(UnifiedLog2026.timestamp.asc())
        ).scalars().all()
    finally:
        session.close()

    # Filter to summaries and user messages worth indexing.
    documents: List[str] = []
    ids: List[str] = []
    metadatas: List[Dict[str, Any]] = []

    for row in rows:
        msg = str(row.message or "").strip()
        if not msg or len(msg) < 10:
            continue

        meta = {}
        if isinstance(row.metadata_json, dict):
            meta = row.metadata_json
        elif isinstance(row.metadata_json, str):
            try:
                meta = json.loads(row.metadata_json)
            except (json.JSONDecodeError, TypeError):
                meta = {}

        sdt = meta.get("sub_data_type", [])
        role = str(row.role or "").strip().lower()
        is_summary = "history_summary" in sdt
        is_user_msg = role == "user" and row.source == "room_ui"

        # User messages have a shorter lookback than summaries.
        if is_user_msg and row.timestamp:
            user_msg_cutoff = datetime.now(timezone.utc) - timedelta(days=_USER_MSG_LOOKBACK_DAYS)
            ts = row.timestamp
            if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < user_msg_cutoff:
                continue

        # Index summaries (compressed multi-message context) and user messages.
        if not is_summary and not is_user_msg:
            continue

        ts_str = row.timestamp.isoformat() if row.timestamp else ""
        doc_id = _stable_id(msg, ts_str)

        # Skip if already indexed.
        if doc_id in ids:
            continue

        doc_type = "summary" if is_summary else "user_message"
        documents.append(msg)
        ids.append(doc_id)
        metadatas.append({
            "timestamp": ts_str,
            "type": doc_type,
            "role": role,
            "room_id": room_id,
        })

    if not documents:
        logger.info("[chat_memory_rag] No new documents to index.")
        return 0

    collection = _get_collection()

    # Check which IDs already exist to avoid re-embedding.
    existing = set()
    try:
        existing_results = collection.get(ids=ids)
        if existing_results and existing_results.get("ids"):
            existing = set(existing_results["ids"])
    except Exception:
        pass  # Collection may be empty.

    new_docs = []
    new_ids = []
    new_metas = []
    for doc, doc_id, meta in zip(documents, ids, metadatas):
        if doc_id not in existing:
            new_docs.append(doc)
            new_ids.append(doc_id)
            new_metas.append(meta)

    if not new_docs:
        logger.info("[chat_memory_rag] All %d documents already indexed.", len(documents))
        return 0

    # Batch add to ChromaDB (it handles embedding internally).
    collection.add(
        documents=new_docs,
        ids=new_ids,
        metadatas=new_metas,
    )

    logger.info(
        "[chat_memory_rag] Indexed %d new document(s) (%d already existed).",
        len(new_docs), len(existing),
    )
    return len(new_docs)


def recall(
    query: str,
    *,
    top_k: int = _DEFAULT_TOP_K,
    room_id: str = "master_room",
) -> List[Dict[str, Any]]:
    """Semantic search over indexed chat summaries.

    Returns a list of matches, each with:
    - text: the matched summary/message
    - timestamp: when it was recorded
    - type: "summary" or "user_message"
    - distance: cosine distance (lower = more similar)
    """
    if not query or not query.strip():
        return []

    try:
        collection = _get_collection()
        results = collection.query(
            query_texts=[query.strip()],
            n_results=top_k,
            where={"room_id": room_id},
        )
    except Exception as e:
        logger.warning("[chat_memory_rag] recall failed: %s", e)
        return []

    if not results or not results.get("documents") or not results["documents"][0]:
        return []

    matches = []
    docs = results["documents"][0]
    metas = results["metadatas"][0] if results.get("metadatas") else [{}] * len(docs)
    distances = results["distances"][0] if results.get("distances") else [0.0] * len(docs)

    for doc, meta, dist in zip(docs, metas, distances):
        matches.append({
            "text": doc,
            "timestamp": meta.get("timestamp", ""),
            "type": meta.get("type", ""),
            "distance": round(dist, 4),
        })

    return matches


def format_recall_for_prompt(matches: List[Dict[str, Any]], max_chars: int = 1500) -> str:
    """Format recall results into a compact string for prompt injection."""
    if not matches:
        return ""

    lines = []
    total = 0
    for m in matches:
        ts = m.get("timestamp", "")[:10]  # Just the date.
        text = m["text"]
        line = f"- [{ts}] {text}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)

    return "\n".join(lines)
