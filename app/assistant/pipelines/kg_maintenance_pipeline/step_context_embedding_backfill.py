"""
Step: context_embedding_backfill

Computes and stores context embeddings for nodes that lack them.
The context embedding captures the node's original_sentence — a much richer
semantic signal than the label-only embedding used for fuzzy search.

Runs in batches to avoid memory pressure.  Each node's embedding is stored
via ChromaDB's upsert (idempotent, safe to re-run).

Session / LLM contract
-----------------------
One short-lived session to load node data.  No LLM calls — only embedding API.
"""
from __future__ import annotations

from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

BATCH_SIZE = 200


def run(ctx: PipelineContext, *, max_nodes: int = 5000) -> dict:
    """
    Backfill context embeddings for nodes missing them.
    Returns {"total_nodes": int, "already_have": int, "embedded": int, "errors": int}.
    """
    from app.assistant.kg.db.knowledge_graph_db import Node
    from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
    from app.assistant.embeddings.embedder import embed_text as _do_embed

    logger.info("[context_embedding_backfill] Starting run_id=%s max_nodes=%d", ctx.run_id, max_nodes)

    session = get_session()
    try:
        rows = (
            session.query(Node.id, Node.label, Node.original_sentence)
            .filter(Node.original_sentence.isnot(None), Node.original_sentence != "")
            .limit(max_nodes + 1000)
            .all()
        )
    finally:
        session.close()

    if not rows:
        logger.info("[context_embedding_backfill] No nodes with original_sentence found")
        return {"total_nodes": 0, "already_have": 0, "embedded": 0, "errors": 0}

    cm = get_chroma_manager()

    all_ids = [str(r.id) for r in rows]
    existing_ids: set[str] = set()
    for start in range(0, len(all_ids), 5000):
        batch = all_ids[start: start + 5000]
        result = cm.node_context_collection.get(ids=batch, include=[])
        existing_ids.update(result.get("ids") or [])

    need_embedding = [(str(r.id), r.label or "", r.original_sentence) for r in rows if str(r.id) not in existing_ids]
    need_embedding = need_embedding[:max_nodes]

    logger.info(
        "[context_embedding_backfill] %d nodes total, %d already have context embeddings, %d to embed",
        len(rows), len(existing_ids), len(need_embedding),
    )

    embedded = 0
    errors = 0

    for i in range(0, len(need_embedding), BATCH_SIZE):
        batch = need_embedding[i: i + BATCH_SIZE]
        for node_id, label, sentence in batch:
            try:
                emb = _do_embed(sentence)
                cm.store_node_context_embedding(node_id, sentence, emb)
                embedded += 1
            except Exception as exc:
                logger.error(
                    "[context_embedding_backfill] Failed node_id=%s label=%s: %s",
                    node_id, label[:30], exc,
                )
                logger.debug("[context_embedding_backfill] Exception details", exc_info=True)
                errors += 1

        logger.info(
            "[context_embedding_backfill] Progress: %d/%d embedded, %d errors",
            embedded, len(need_embedding), errors,
        )

    result = {
        "total_nodes": len(rows),
        "already_have": len(existing_ids),
        "embedded": embedded,
        "errors": errors,
    }
    logger.info("[context_embedding_backfill] Done: %s", result)
    return result
