"""
Central node-merge helper and dependent-id registry.

Every node merge (loser → winner) must:

  1. Reroute loser's edges to winner (dropping duplicates).
  2. Rebind every dependent-table pointer from loser_id to winner_id.
  3. Write a ``kg_merge_log`` row with full snapshots, so the merge can be
     undone later.
  4. Delete the loser node.

Point 2 is what this module primarily owns — previously each merge call site
knew only about edges, so any other table holding ``source_node_id`` /
``node_id`` was silently orphaned at merge time (most visibly: entity_cards).

``NODE_ID_REFERENCES`` is the single source of truth for "tables that store a
KG node id." Add new entries here when a new table starts referencing nodes;
all merge paths pick them up automatically.

Deliberately EXCLUDED from the registry (do not rebind):
  - ``kg_edge_metadata.(source_id, target_id)``: handled by edge rerouting.
  - ``kg_chat_merged_node_ref.node_id``: historical breadcrumb — rewriting
    would misrepresent what the extractor decided at merge time.
  - ``kg_maintenance_finding.(primary_node_id, secondary_node_id)``: findings
    are time-stamped observations about specific nodes; they should not be
    silently rewritten. Resolve or dismiss pending findings through the
    maintenance UI instead.
  - ``kg_node_evidence.source_id`` / ``kg_edge_evidence.source_id``: these
    point at message rows (unified_log, etc.), NOT at KG nodes.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from app.assistant.database.kg_merge_log import KGMergeLog
from app.assistant.database.kg_node_verdict import KGNodeVerdict
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now
from app.models.base import Base

logger = get_logger(__name__)


# (table_name, column_name) pairs that store a kg_node_metadata.id and should
# be rebound from loser → winner during a merge. The rebind is a simple
# ``UPDATE <table> SET <column> = :winner WHERE <column> = :loser``. Counts
# and rows_touched are returned so the merge log can record what happened.
NODE_ID_REFERENCES: List[Tuple[str, str]] = [
    ("entity_cards", "source_node_id"),
    ("node_taxonomy_links", "node_id"),
    ("node_taxonomy_review_queue", "node_id"),
    ("event_nodes", "node_id"),
    ("event_nodes", "parent_event_node_id"),
    ("event_nodes", "root_event_node_id"),
    ("event_node_sources", "event_node_id"),
    ("kg_node_evidence", "node_id"),
    ("claim_proposal_node", "resolved_node_id"),
    ("claim_proposal_edge", "source_node_id"),
    ("claim_proposal_edge", "target_node_id"),
]


def ensure_merge_log_table(session: Session) -> None:
    """Create the kg_merge_log table if it doesn't exist yet. Idempotent.

    Uses the session's existing connection (not the engine) so the DDL runs
    on the same SQLite connection that holds the in-flight transaction.
    Calling ``session.get_bind()`` returns the engine, which would open a
    NEW connection from the pool — that fresh connection then waits forever
    for the write lock the current session is already holding (self-
    deadlock). Reusing ``session.connection()`` avoids the second-connection
    trap entirely.
    """
    Base.metadata.create_all(
        session.connection(),
        tables=[KGMergeLog.__table__],
        checkfirst=True,
    )


def snapshot_node(node) -> Dict[str, Any]:
    """Full pre-merge snapshot of a Node row, JSON-safe for the merge log."""
    def _iso(dt):
        if dt is None:
            return None
        if hasattr(dt, "isoformat"):
            return dt.isoformat()
        return str(dt)

    return {
        "id": str(node.id),
        "label": node.label,
        "node_type": node.node_type,
        "description": node.description,
        "aliases": list(node.aliases or []),
        "hash_tags": list(node.hash_tags or []),
        "semantic_label": node.semantic_label,
        "goal_status": node.goal_status,
        "valid_during": node.valid_during,
        "category": node.category,
        "attributes": node.attributes,
        "start_date": _iso(node.start_date),
        "end_date": _iso(node.end_date),
        "start_date_confidence": node.start_date_confidence,
        "end_date_confidence": node.end_date_confidence,
        "start_date_prose": getattr(node, "start_date_prose", None),
        "end_date_prose": getattr(node, "end_date_prose", None),
        "confidence": node.confidence,
        "importance": node.importance,
        "source": node.source,
        "pagerank_score": node.pagerank_score,
        "original_sentence": getattr(node, "original_sentence", None),
        "created_at": _iso(getattr(node, "created_at", None)),
        "updated_at": _iso(getattr(node, "updated_at", None)),
    }


def snapshot_edge(edge) -> Dict[str, Any]:
    """Full snapshot of an Edge row. Used for dropped-duplicate edges so
    unmerge can re-INSERT them."""
    def _iso(dt):
        if dt is None:
            return None
        if hasattr(dt, "isoformat"):
            return dt.isoformat()
        return str(dt)

    return {
        "id": str(edge.id),
        "source_id": str(edge.source_id) if edge.source_id else None,
        "target_id": str(edge.target_id) if edge.target_id else None,
        "relationship_type": edge.relationship_type,
        "attributes": edge.attributes,
        "sentence": getattr(edge, "sentence", None),
        "confidence": edge.confidence,
        "importance": getattr(edge, "importance", None),
        "source": edge.source,
    }


def reroute_edges(
    session: Session, loser_id: str, winner_id: str
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """
    Move loser's edges to winner. Drop duplicates (when winner already has
    the same (source, target, relationship_type)).

    Returns (rerouted_edge_ids, dropped_edge_snapshots).

    Entirely raw-SQL — intentionally does NOT load Edge ORM instances. If we
    loaded via ORM, SQLAlchemy's back_populates would populate
    ``loser.outgoing_edges`` / ``incoming_edges`` with those instances, and
    any later ``session.flush`` would try to re-persist them against loser
    (reverting our raw-SQL rewrites). Bypassing the ORM for edge writes is
    the clean way to avoid that whole class of drift.
    """
    loser_id = str(loser_id)
    winner_id = str(winner_id)

    # Winner's existing (src, tgt, rel) keys — needed to detect duplicates.
    existing_keys: set[tuple[str, str, str]] = set()
    for row in session.execute(
        text(
            "SELECT source_id, target_id, relationship_type FROM kg_edge_metadata "
            "WHERE source_id = :winner OR target_id = :winner"
        ),
        {"winner": winner_id},
    ).fetchall():
        existing_keys.add((str(row[0]), str(row[1]), row[2]))

    # All of loser's edges — every column (we need snapshots for the dropped ones).
    loser_edge_rows = session.execute(
        text(
            "SELECT id, source_id, target_id, relationship_type, "
            "attributes, sentence, "
            "confidence, importance, source "
            "FROM kg_edge_metadata "
            "WHERE source_id = :loser OR target_id = :loser"
        ),
        {"loser": loser_id},
    ).mappings().fetchall()

    rerouted: List[str] = []
    dropped_snapshots: List[Dict[str, Any]] = []

    for row in loser_edge_rows:
        edge_id = str(row["id"])
        src = str(row["source_id"]) if row["source_id"] else None
        tgt = str(row["target_id"]) if row["target_id"] else None
        rel = row["relationship_type"]

        new_src = winner_id if src == loser_id else src
        new_tgt = winner_id if tgt == loser_id else tgt

        # Self-loop after collapse, or duplicate vs. existing winner edge.
        if new_src == new_tgt or (new_src, new_tgt, rel) in existing_keys:
            dropped_snapshots.append({
                "id": edge_id,
                "source_id": src,
                "target_id": tgt,
                "relationship_type": rel,
                "attributes": row["attributes"],
                "sentence": row["sentence"],
                "confidence": row["confidence"],
                "importance": row["importance"],
                "source": row["source"],
            })
            session.execute(
                text("DELETE FROM kg_edge_metadata WHERE id = :eid"),
                {"eid": edge_id},
            )
            continue

        session.execute(
            text(
                "UPDATE kg_edge_metadata "
                "SET source_id = :new_src, target_id = :new_tgt "
                "WHERE id = :eid"
            ),
            {"new_src": new_src, "new_tgt": new_tgt, "eid": edge_id},
        )
        existing_keys.add((new_src, new_tgt, rel))
        rerouted.append(edge_id)

    session.flush()
    return rerouted, dropped_snapshots


def rebind_node_references(
    session: Session, loser_id: str, winner_id: str
) -> List[Dict[str, Any]]:
    """
    Walk NODE_ID_REFERENCES and rewrite every dependent pointer from
    loser_id to winner_id. Returns a list of rebind records for the merge log:

        [{"table": "entity_cards", "column": "source_node_id",
          "row_ids": [...], "count": N}, ...]

    Row-level ids are captured (up to a safety cap) so unmerge can target
    exactly the rows that were rewritten, not every row that currently points
    at the winner. A table that doesn't exist is skipped quietly — rebinding
    only runs against tables that exist in this DB.
    """
    loser_id = str(loser_id)
    winner_id = str(winner_id)

    # Use the session's transaction (not a fresh engine connection) to list
    # tables. sqlalchemy's Inspector opens its own connection, which on
    # SQLite can end up rolling back in-flight raw SQL writes from the
    # session's transaction. Read sqlite_master directly instead.
    existing_tables = {
        str(r[0])
        for r in session.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).fetchall()
    }

    rebinds: List[Dict[str, Any]] = []
    for table_name, column_name in NODE_ID_REFERENCES:
        if table_name not in existing_tables:
            continue

        # Discover the PK shape so we can (a) capture row identity for unmerge
        # and (b) pre-resolve composite-PK conflicts. Some junction tables
        # (e.g. node_taxonomy_links) use a composite PK like (node_id,
        # taxonomy_id) with no surrogate `id` column — the old code SELECT'd
        # `id` blindly and crashed.
        col_info = session.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        # PRAGMA returns: cid, name, type, notnull, dflt_value, pk(0 or position)
        pk_cols = [str(r[1]) for r in col_info if int(r[5]) > 0]
        has_surrogate_id = any(str(r[1]) == "id" for r in col_info)

        # Pre-clear conflicting rows on composite-PK junction tables.
        # If the rebind column is part of a composite PK, then UPDATE-ing it
        # from loser to winner can collide with an existing (winner, ...) row.
        # Drop the loser-side row in that case — the winner already represents
        # the same junction, and the winner's row wins (its data is canonical
        # post-merge). For single-PK tables this is a no-op.
        if column_name in pk_cols and len(pk_cols) > 1:
            other_pk_cols = [c for c in pk_cols if c != column_name]
            other_cols_sql = ", ".join(other_pk_cols)
            session.execute(
                text(
                    f"DELETE FROM {table_name} "
                    f"WHERE {column_name} = :loser "
                    f"AND ({other_cols_sql}) IN "
                    f"(SELECT {other_cols_sql} FROM {table_name} WHERE {column_name} = :winner)"
                ),
                {"loser": loser_id, "winner": winner_id},
            )

        # Capture row ids for unmerge if the table has a surrogate `id`.
        # Composite-PK tables: skip exact capture; unmerge falls back to
        # (table, column, winner→loser) inverse-update. The merge log records
        # row_ids_truncated=True for that case.
        if has_surrogate_id:
            pk_rows = session.execute(
                text(f"SELECT id FROM {table_name} WHERE {column_name} = :loser"),
                {"loser": loser_id},
            ).fetchall()
            row_ids = [str(r[0]) for r in pk_rows]
            if not row_ids:
                continue
        else:
            # Count remaining loser-rows after conflict pre-clear.
            n_remaining = session.execute(
                text(f"SELECT COUNT(*) FROM {table_name} WHERE {column_name} = :loser"),
                {"loser": loser_id},
            ).scalar() or 0
            if n_remaining == 0:
                continue
            row_ids = []

        result = session.execute(
            text(
                f"UPDATE {table_name} SET {column_name} = :winner "
                f"WHERE {column_name} = :loser"
            ),
            {"winner": winner_id, "loser": loser_id},
        )
        count = result.rowcount if result.rowcount is not None else len(row_ids)

        # Row ids captured only up to 500; beyond that, rely on (table, column,
        # old→new) inverse-update during unmerge. This is a pragmatic ceiling
        # to keep merge log entries from blowing up.
        row_ids_for_log = row_ids[:500]
        truncated = (not has_surrogate_id) or (len(row_ids) > len(row_ids_for_log))

        rebinds.append({
            "table": table_name,
            "column": column_name,
            "row_ids": row_ids_for_log,
            "row_ids_truncated": truncated,
            "count": count,
        })
        logger.info(
            "[node_merge] rebound %d row(s) in %s.%s from %s → %s",
            count, table_name, column_name, loser_id[:8], winner_id[:8],
        )

    session.flush()
    return rebinds


def _table_exists(session: Session, table_name: str) -> bool:
    return session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :t"),
        {"t": table_name},
    ).fetchone() is not None


def migrate_section_tags(
    session: Session, loser_id: str, winner_id: str
) -> Dict[str, Any]:
    """
    Move loser's ``kg_node_section_tag`` rows to winner where winner lacks
    that (namespace, section_name); rows winner already covers are left on
    loser to FK-cascade away with it. Not in NODE_ID_REFERENCES because a
    blind rebind would hit the (node_id, namespace, section_name) unique
    constraint whenever both nodes carry the same tag.

    Returns {"snapshots": [all loser tag rows pre-migration],
             "migrated_ids": [...], "cascaded_count": N}.
    """
    loser_id = str(loser_id)
    winner_id = str(winner_id)
    empty = {"snapshots": [], "migrated_ids": [], "cascaded_count": 0}
    if not _table_exists(session, "kg_node_section_tag"):
        return empty

    rows = session.execute(
        text(
            "SELECT id, node_id, namespace, section_name, tagged_at, "
            "tagger_version, dropped_at, dropped_at_node_content_hash, "
            "dropped_by_version "
            "FROM kg_node_section_tag WHERE node_id = :loser"
        ),
        {"loser": loser_id},
    ).mappings().fetchall()
    if not rows:
        return empty

    snapshots = [{k: (str(v) if v is not None else None) for k, v in r.items()} for r in rows]

    migratable = session.execute(
        text(
            "SELECT t.id FROM kg_node_section_tag t WHERE t.node_id = :loser "
            "AND NOT EXISTS (SELECT 1 FROM kg_node_section_tag k2 "
            "WHERE k2.node_id = :winner AND k2.namespace = t.namespace "
            "AND k2.section_name = t.section_name)"
        ),
        {"loser": loser_id, "winner": winner_id},
    ).fetchall()
    migrated_ids = [str(r[0]) for r in migratable]
    for tag_id in migrated_ids:
        session.execute(
            text("UPDATE kg_node_section_tag SET node_id = :winner WHERE id = :tid"),
            {"winner": winner_id, "tid": tag_id},
        )
    session.flush()

    cascaded = len(rows) - len(migrated_ids)
    if migrated_ids or cascaded:
        logger.info(
            "[node_merge] section tags: migrated %d, leaving %d to cascade (%s → %s)",
            len(migrated_ids), cascaded, loser_id[:8], winner_id[:8],
        )
    return {"snapshots": snapshots, "migrated_ids": migrated_ids, "cascaded_count": cascaded}


def supersede_verdicts_for_node(session: Session, node_id: str, *, reason: str) -> int:
    """
    Mark every active kg_node_verdict row naming ``node_id`` as superseded.
    Supersede, NOT rebind: rewriting (X, fold) → (X, keep) could wrongly
    suppress a real (X, keep) duplicate question later. Returns rows updated.
    """
    if not _table_exists(session, "kg_node_verdict"):
        return 0
    node_id = str(node_id)
    count = (
        session.query(KGNodeVerdict)
        .filter(or_(KGNodeVerdict.node_id_a == node_id, KGNodeVerdict.node_id_b == node_id))
        .filter(KGNodeVerdict.superseded_at.is_(None))
        .update(
            {KGNodeVerdict.superseded_at: utc_now(), KGNodeVerdict.superseded_reason: reason},
            synchronize_session=False,
        )
    )
    if count:
        logger.info(
            "[node_merge] superseded %d verdict(s) naming %s (%s)",
            count, node_id[:8], reason,
        )
    return count


def delete_node_chroma_vectors(node_id: str) -> bool:
    """
    Remove a node's label + context embeddings from Chroma. Best-effort by
    necessity — Chroma is not transactional with SQLite — so failures are
    logged at ERROR (ghost vectors feed the duplicate scan false candidates
    until swept) and reported via the return value, never raised.
    """
    try:
        from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
        cm = get_chroma_manager()
        cm.delete_node_embedding(node_id)
        cm.delete_node_context_embedding(node_id)
        return True
    except Exception as exc:
        logger.error(
            "[node_merge] Chroma cleanup failed for node %s — ghost vectors "
            "remain in the label/context collections: %s", node_id, exc,
        )
        return False


def record_merge(
    session: Session,
    *,
    loser_snapshot: Dict[str, Any],
    winner_pre_snapshot: Dict[str, Any],
    rerouted_edge_ids: List[str],
    dropped_edge_snapshots: List[Dict[str, Any]],
    rebinds: List[Dict[str, Any]],
    merge_actor: str,
    notes: str | None = None,
) -> str:
    """Insert a merge-log row with the full undo playbook. Returns its id."""
    ensure_merge_log_table(session)
    row = KGMergeLog(
        id=str(uuid.uuid4()),
        loser_id=str(loser_snapshot["id"]),
        winner_id=str(winner_pre_snapshot["id"]),
        merge_actor=merge_actor,
        notes=notes,
        loser_snapshot_json=loser_snapshot,
        winner_pre_snapshot_json=winner_pre_snapshot,
        rerouted_edge_ids_json=rerouted_edge_ids,
        dropped_edge_snapshots_json=dropped_edge_snapshots,
        rebinds_json=rebinds,
    )
    session.add(row)
    session.flush()
    logger.info(
        "[node_merge] recorded merge_log id=%s loser=%s winner=%s "
        "rerouted=%d dropped=%d rebinds=%d",
        row.id, row.loser_id[:8], row.winner_id[:8],
        len(rerouted_edge_ids), len(dropped_edge_snapshots), len(rebinds),
    )
    return row.id


def merge_nodes_in_session(
    session: Session,
    *,
    loser_node,
    winner_node,
    merge_actor: str,
    notes: str | None = None,
    winner_pre_snapshot: Dict[str, Any] | None = None,
) -> str:
    """
    Orchestrate the full merge in an already-open session:
      1. Snapshot loser (winner snapshot optional — caller may have captured
         it BEFORE applying field-merge mutations; if not provided we snapshot
         the current winner state, which already reflects the merge).
      2. Reroute edges (loser → winner), capturing dropped duplicates.
      3. Rebind dependent-table pointers via the registry.
      4. Migrate loser's section tags winner lacks; supersede verdicts
         naming loser.
      5. Write merge-log row.
      6. Delete the loser node + its Chroma label/context embeddings.

    Does NOT commit — caller controls transaction boundary. The Chroma
    cleanup therefore runs pre-commit: if the caller rolls back, the loser
    keeps its SQLite row but has lost its vectors. That failure mode
    self-heals (label re-embeds lazily on read; context backfills nightly),
    whereas the inverse — committing the merge and leaving loser's vectors
    behind — mints permanent ghost candidates for the duplicate scan.
    Returns the merge_log row id.
    """
    loser_id = str(loser_node.id)
    winner_id = str(winner_node.id)

    loser_snapshot = snapshot_node(loser_node)
    if winner_pre_snapshot is None:
        winner_pre_snapshot = snapshot_node(winner_node)

    rerouted, dropped = reroute_edges(session, loser_id, winner_id)
    rebinds = rebind_node_references(session, loser_id, winner_id)

    tag_migration = migrate_section_tags(session, loser_id, winner_id)
    loser_snapshot["section_tags"] = tag_migration["snapshots"]
    if tag_migration["migrated_ids"]:
        # Same record shape as rebind_node_references entries so a future
        # unmerge can inverse-update these rows alongside the registry tables.
        rebinds.append({
            "table": "kg_node_section_tag",
            "column": "node_id",
            "row_ids": tag_migration["migrated_ids"],
            "row_ids_truncated": False,
            "count": len(tag_migration["migrated_ids"]),
        })

    supersede_verdicts_for_node(
        session, loser_id, reason=f"node merged into {winner_id}"
    )

    # The winner's identity grew (loser's edges + fields folded in): its
    # dupe-scan watermark is stale — the next scan must re-pair it
    # (audit P1.2).
    session.execute(
        text(
            "UPDATE kg_node_metadata SET last_dupe_scanned_at = NULL "
            "WHERE id = :winner"
        ),
        {"winner": winner_id},
    )

    log_id = record_merge(
        session,
        loser_snapshot=loser_snapshot,
        winner_pre_snapshot=winner_pre_snapshot,
        rerouted_edge_ids=rerouted,
        dropped_edge_snapshots=dropped,
        rebinds=rebinds,
        merge_actor=merge_actor,
        notes=notes,
    )

    # Delete the loser via raw SQL rather than ORM. With back_populates on
    # Node.outgoing_edges / incoming_edges, ORM delete walks the relationship
    # collection and tries to NULL-out the FKs of any in-memory edges that
    # were linked to loser. After reroute the DB no longer has any edges
    # referencing loser, but the ORM's cached collections may still hold the
    # pre-reroute Edge instances, and SQLAlchemy's nullify path tries to
    # flush source_id=NULL updates that violate NOT NULL. Raw SQL DELETE
    # bypasses the ORM entirely.
    #
    # NOTE: The FK + ON DELETE CASCADE that previously cleaned up stragglers
    # was dropped on 2026-05-10 (no-mirror migration). reroute_edges() above
    # is now solely responsible for ensuring zero edges reference the loser
    # before this DELETE — otherwise they would become orphaned rows.
    session.expunge(loser_node)
    session.execute(
        text("DELETE FROM kg_node_metadata WHERE id = :id"),
        {"id": loser_id},
    )
    session.flush()

    delete_node_chroma_vectors(loser_id)
    return log_id
