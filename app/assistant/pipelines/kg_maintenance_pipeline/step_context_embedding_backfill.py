"""
Step: context_embedding_backfill

Computes and stores the embeddings nodes are missing, for BOTH Chroma
collections:
  - node_context_embeddings — the node's original_sentence (rich signal)
  - node_embeddings         — the node's label (identity signal)

The label half was added 2026-06-10 (audit P1.5(1) prerequisite): the
node_embeddings collection was found EMPTY in the live store — an old
embedding-schema reset wiped it and the promoter's embed-at-write only
covered context vectors — leaving the duplicate scan's tier 3 and the
promoter's entity semantic tier running on context vectors alone. This
step self-heals that in-process on the nightly kg_maintenance run (no
second PersistentClient touching the chroma dir).

Runs in batches to avoid memory pressure.  Each embedding is stored via
ChromaDB's upsert (idempotent, safe to re-run).

Session / LLM contract
-----------------------
One short-lived session to load node data.  No LLM calls — only embedding API.
"""
from __future__ import annotations

from app.assistant.kg import failure_ledger
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

BATCH_SIZE = 200
CONTEXT_KIND = "kg_context_embedding"
LABEL_KIND = "kg_label_embedding"


def _missing_ids(collection, all_ids: list[str]) -> set[str]:
    """Ids from ``all_ids`` that have no vector in ``collection``."""
    existing: set[str] = set()
    for start in range(0, len(all_ids), 5000):
        batch = all_ids[start: start + 5000]
        result = collection.get(ids=batch, include=[])
        existing.update(result.get("ids") or [])
    return set(all_ids) - existing


def run(ctx: PipelineContext, *, max_nodes: int = 5000) -> dict:
    """
    Backfill label + context embeddings for nodes missing them.
    Returns {"total_nodes": int, "context_already_have": int,
    "context_embedded": int, "label_already_have": int,
    "label_embedded": int}.
    """
    from app.assistant.kg.db.knowledge_graph_db import Node
    from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
    from app.assistant.embeddings.embedder import embed_text as _do_embed

    logger.info("[context_embedding_backfill] Starting run_id=%s max_nodes=%d", ctx.run_id, max_nodes)

    session = get_session()
    try:
        rows = (
            session.query(Node.id, Node.label, Node.original_sentence,
                          Node.identity_sentence)
            .all()
        )
    finally:
        session.close()

    if not rows:
        logger.info("[context_embedding_backfill] No nodes found")
        return {"total_nodes": 0, "context_already_have": 0, "context_embedded": 0,
                "label_already_have": 0, "label_embedded": 0}

    cm = get_chroma_manager()

    # A failed embed or Chroma write stops the run; the remaining nodes stay
    # missing and are picked up next run. A node that keeps failing is set
    # aside for review.
    def _embed_each(kind: str, items: list[tuple[str, str, str]], store) -> int:
        done = 0
        for i in range(0, len(items), BATCH_SIZE):
            for node_id, label, text in items[i: i + BATCH_SIZE]:
                try:
                    store(node_id, text, _do_embed(text))
                except Exception as exc:
                    failure_ledger.record_failure(kind, node_id, exc, label=label)
                    raise
                failure_ledger.clear(kind, node_id)
                done += 1
            logger.info("[context_embedding_backfill] %s progress: %d/%d", kind, done, len(items))
        return done

    # ── Context embeddings (identity head + original observation) ───────
    from app.assistant.kg.chroma.embedding_sync import compose_context_text
    ctx_rows = [
        (str(r.id), r.label or "",
         compose_context_text(r.identity_sentence, r.original_sentence))
        for r in rows
        if compose_context_text(r.identity_sentence, r.original_sentence)
    ]
    ctx_ids = [nid for nid, _, _ in ctx_rows]
    ctx_missing = _missing_ids(cm.node_context_collection, ctx_ids)
    ctx_aside = failure_ledger.set_aside_ids(CONTEXT_KIND)
    need_context = [t for t in ctx_rows if t[0] in ctx_missing and t[0] not in ctx_aside][:max_nodes]
    context_embedded = _embed_each(CONTEXT_KIND, need_context, cm.store_node_context_embedding)

    # ── Label embeddings ─────────────────────────────────────────────────
    lbl_rows = [(str(r.id), r.label, r.label) for r in rows if r.label and str(r.label).strip()]
    lbl_ids = [nid for nid, _, _ in lbl_rows]
    lbl_missing = _missing_ids(cm.node_collection, lbl_ids)
    lbl_aside = failure_ledger.set_aside_ids(LABEL_KIND)
    need_label = [t for t in lbl_rows if t[0] in lbl_missing and t[0] not in lbl_aside][:max_nodes]
    label_embedded = _embed_each(LABEL_KIND, need_label, cm.store_node_embedding)

    result = {
        "total_nodes": len(rows),
        "context_already_have": len(ctx_ids) - len(ctx_missing),
        "context_embedded": context_embedded,
        "label_already_have": len(lbl_ids) - len(lbl_missing),
        "label_embedded": label_embedded,
    }
    logger.info("[context_embedding_backfill] Done: %s", result)
    return result
