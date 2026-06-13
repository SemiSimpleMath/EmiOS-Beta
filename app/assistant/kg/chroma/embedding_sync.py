"""KG embedding index freshness — three speeds (fragility review #4).

Chroma is a DERIVED INDEX (embeddings of node label / identity sentence;
SQLite is the only truth) and the embedder is local MiniLM — rebuilds are
free. The risk is a stale index on the decision path: ghost vectors feed
dead candidate ids, invisible new nodes invite twin-minting by the dup
scan, stale renames miss matches. Three freshness speeds:

  instant — ORM-event chokepoint: Node insert/update(label,sentences)/
            delete queue embedding work in the session; the session's
            after_commit drains it inline. No mutation path can forget
            chroma, because forgetting is no longer expressible.
  hourly  — sync_kg_embeddings(mode="diff"): full reconciler (ghosts
            removed, missing embedded, text-drift re-embedded by
            comparing stored metadata to current sqlite text).
  nightly — sync_kg_embeddings(mode="full"): re-embed everything;
            drift cannot survive 24h by construction.

The reconciler covers the label + identity collections; the context
collection (sentence + edge text) keeps its dedicated backfill step but
gets ghost-cleaned here.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_STATUS_FILENAME = "kg_embedding_sync_status.json"
_SESSION_QUEUE_KEY = "kg_embedding_sync_queue"
_registered = False
_listeners: list = []  # (target, event_name, fn) — for unregister in tests


def compose_context_text(identity_sentence, original_sentence) -> str:
    """The context (aboutness) embedding text: definite description as the
    anchored head + the original observation as content. Before identity
    sentences existed, context vectors were ONLY the accidental first
    observation; leading with WHO the node is pulls same-referent nodes
    together and pushes different referents apart without losing content."""
    parts = [
        str(p).strip() for p in (identity_sentence, original_sentence)
        if p and str(p).strip()
    ]
    return " ".join(parts)


# ── reconciler ────────────────────────────────────────────────────────────

def sync_kg_embeddings(*, mode: str = "diff", max_embeds: Optional[int] = None) -> Dict[str, Any]:
    """Reconcile the chroma label + identity collections against sqlite.

    mode="diff": embed only missing/stale, remove ghosts.
    mode="full": re-embed every row regardless (the nightly floor).
    Idempotent either way; safe to run any time.

    max_embeds bounds the embeds done in ONE run (None = unbounded). A big
    backlog — e.g. the self-heal dropping a collection, leaving ~6.6k
    vectors to rebuild — would otherwise run ~40min and breach the hourly
    reconciler's watchdog. With a budget the run stays short and the
    remaining work converges over subsequent runs (the diff is idempotent;
    the next run picks up whatever is still missing). Ghost removal is
    always completed (cheap deletes, no embeds).
    """
    from app.assistant.embeddings.embedder import embed_text
    from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
    from app.models.db_manager import get_db_manager
    from sqlalchemy import text as sql_text

    if mode not in ("diff", "full"):
        raise ValueError(f"mode must be diff|full, got {mode!r}")

    with get_db_manager().read_session() as s:
        rows = s.execute(sql_text(
            "SELECT id, label, identity_sentence, original_sentence "
            "FROM kg_node_metadata"
        )).fetchall()
    sql_nodes = {str(r[0]): (r[1] or "", r[2], r[3]) for r in rows}

    cm = get_chroma_manager()

    def _chroma_state(collection):
        res = collection.get()  # full scan — fine at this graph size
        ids = [str(i) for i in (res.get("ids") or [])]
        metas = res.get("metadatas") or [{}] * len(ids)
        return dict(zip(ids, metas))

    label_state = _chroma_state(cm.node_collection)
    identity_state = _chroma_state(cm.node_identity_collection)
    context_state = _chroma_state(cm.node_context_collection)

    counts = {
        "mode": mode, "nodes": len(sql_nodes),
        "ghosts_removed": 0, "labels_embedded": 0,
        "identities_embedded": 0, "identities_removed": 0,
        "contexts_embedded": 0, "budget_hit": False,
    }

    # Ghosts: chroma ids with no sqlite row — remove from ALL collections.
    for cid in set(label_state) | set(identity_state) | set(context_state):
        if cid not in sql_nodes:
            cm.delete_node_embedding(cid)
            cm.delete_node_context_embedding(cid)
            cm.delete_node_identity_embedding(cid)
            counts["ghosts_removed"] += 1

    for nid, (label, identity_sentence, original_sentence) in sql_nodes.items():
        # Per-run embed budget: stop starting new nodes once hit, so a large
        # backlog converges over multiple runs instead of breaching the
        # watchdog. Checked at node granularity so a node is fully done or
        # not started.
        if max_embeds is not None and (
            counts["labels_embedded"] + counts["identities_embedded"]
            + counts["contexts_embedded"]
        ) >= max_embeds:
            counts["budget_hit"] = True
            break

        # Label collection: missing, text-drifted, or full mode.
        meta = label_state.get(nid)
        stale = meta is None or (meta or {}).get("label") != label
        if label.strip() and (stale or mode == "full"):
            cm.store_node_embedding(nid, label, embed_text(label))
            counts["labels_embedded"] += 1

        # Identity collection: mirrors identity_sentence exactly.
        imeta = identity_state.get(nid)
        if identity_sentence and identity_sentence.strip():
            preview = identity_sentence[:300]
            istale = imeta is None or (imeta or {}).get("identity_sentence") != preview
            if istale or mode == "full":
                cm.store_node_identity_embedding(
                    nid, identity_sentence, embed_text(identity_sentence))
                counts["identities_embedded"] += 1
        elif imeta is not None:
            cm.delete_node_identity_embedding(nid)
            counts["identities_removed"] += 1

        # Context (aboutness) collection: identity-sentence head + original
        # observation. Compared via its stored preview so ANY text drift
        # (identity regen, sentence canonicalization) re-anchors it — the
        # diff covers everything the full mode does, which keeps full mode
        # a weekly floor (20k embeds ≈ 2h+ on this hardware, measured).
        ctx_text = compose_context_text(identity_sentence, original_sentence)
        if ctx_text:
            cmeta = context_state.get(nid)
            cstale = cmeta is None or (cmeta or {}).get("context_preview") != ctx_text[:200]
            if cstale or mode == "full":
                cm.store_node_context_embedding(nid, ctx_text, embed_text(ctx_text))
                counts["contexts_embedded"] += 1

    _write_status(counts)
    logger.info("[embedding_sync] %s", counts)
    return counts


def _write_status(counts: Dict[str, Any]) -> None:
    from app.assistant.utils.path_utils import get_data_dir
    from app.assistant.utils.time_utils import utc_now

    try:
        path = get_data_dir() / _STATUS_FILENAME
        payload = {**counts, "ran_at_utc": utc_now().isoformat()}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error("[embedding_sync] status write failed: %s", e)


def read_sync_status() -> Optional[Dict[str, Any]]:
    from app.assistant.utils.path_utils import get_data_dir

    path = get_data_dir() / _STATUS_FILENAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("[embedding_sync] status read failed: %s", e)
        return None


# ── ORM-event chokepoint ──────────────────────────────────────────────────

def register_kg_embedding_sync() -> None:
    """Attach the chokepoint: every ORM mutation of a Node's identity text
    queues embedding work in its session; after_commit drains it inline.

    Production-process only (registered from initialize_system, gated off
    test DBs). Embedding failures post-commit log ERROR — the hourly diff
    and nightly full rebuild heal; nothing here can break the commit.

    NOTE: core UPDATE statements (sql_update) bypass mapper events by
    design — paths that use them (identity refresh) embed explicitly.
    """
    global _registered
    if _registered:
        return

    from sqlalchemy import event, inspect
    from sqlalchemy.orm import Session as _SASession, object_session

    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    def _queue(target, op: str) -> None:
        session = object_session(target)
        if session is None:
            return
        session.info.setdefault(_SESSION_QUEUE_KEY, []).append({
            "op": op,
            "node_id": target.id,
            "label": target.label,
            "identity_sentence": getattr(target, "identity_sentence", None),
        })

    def _on_insert(mapper, connection, target):
        _queue(target, "upsert")

    def _on_update(mapper, connection, target):
        state = inspect(target)
        for field in ("label", "original_sentence", "identity_sentence"):
            try:
                if state.get_history(field, True).has_changes():
                    _queue(target, "upsert")
                    return
            except Exception:
                _queue(target, "upsert")
                return

    def _on_delete(mapper, connection, target):
        _queue(target, "delete")

    def _drain(session):
        items = session.info.pop(_SESSION_QUEUE_KEY, None)
        if not items:
            return
        try:
            from app.assistant.embeddings.embedder import embed_text
            from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
            cm = get_chroma_manager()
            for item in items:
                nid = item["node_id"]
                if item["op"] == "delete":
                    cm.delete_node_embedding(nid)
                    cm.delete_node_context_embedding(nid)
                    cm.delete_node_identity_embedding(nid)
                    continue
                label = (item.get("label") or "").strip()
                if label:
                    cm.store_node_embedding(nid, label, embed_text(label))
                sentence = (item.get("identity_sentence") or "").strip()
                if sentence:
                    cm.store_node_identity_embedding(nid, sentence, embed_text(sentence))
        except Exception as e:
            logger.error(
                "[embedding_sync] post-commit embed failed (%d item(s)) — "
                "hourly diff heals: %s", len(items), e,
            )

    def _discard(session):
        session.info.pop(_SESSION_QUEUE_KEY, None)

    wiring = [
        (Node, "after_insert", _on_insert),
        (Node, "after_update", _on_update),
        (Node, "after_delete", _on_delete),
        (_SASession, "after_commit", _drain),
        (_SASession, "after_rollback", _discard),
    ]
    for target, name, fn in wiring:
        event.listen(target, name, fn)
        _listeners.append((target, name, fn))

    _registered = True
    logger.info("[embedding_sync] ORM chokepoint registered")


def unregister_kg_embedding_sync() -> None:
    """Detach the chokepoint listeners (test isolation only)."""
    global _registered
    from sqlalchemy import event

    while _listeners:
        target, name, fn = _listeners.pop()
        try:
            event.remove(target, name, fn)
        except Exception as e:
            logger.error("[embedding_sync] listener remove failed: %s", e)
    _registered = False
