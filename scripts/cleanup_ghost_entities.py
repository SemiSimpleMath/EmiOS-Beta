"""cleanup_ghost_entities.py — bulk cleanup of resolver-failure ghost Entities.

From the 32 audit-flagged Entity nodes (2026-05-11 audit):
  - 3 MERGE into canonical named entities (Peter, Annika, Katy)
  - ~21 DELETE (pseudo-entities + collective placeholders)
  - 6 KEEP (real entities with degraded sentences only, real unnamed people)

For each MERGE:
  - Reroute the loser's edges onto the winner (preserves attached State facts)
  - Rebind dependent-table refs (entity_cards, taxonomy_links, kg_node_evidence, etc.)
  - Mark related kg_maintenance_finding rows as 'executed'
  - Delete the loser node

For each DELETE:
  - Delete the node's edges (FK CASCADE was dropped 2026-05-10, so manual)
  - Delete dependent rows in registry tables (kg_node_evidence, entity_cards, etc.)
  - Mark related kg_maintenance_finding rows as 'dismissed'
  - Delete the node row

Modes:
  --audit       Print the action plan + counts. No DB ops.
  --commit      Apply. Requires --yes-write + recent emi.db.bak.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
DB_PATH = str(REPO / "emi.db")
BACKUP_PATH = str(REPO / "emi.db.bak")
BACKUP_FRESH_SECONDS = 60 * 60

# (loser_label, winner_label) — winner must already exist as canonical Entity.
MERGE_ACTIONS = [
    ("Jukka Virtanen's Son", "Peter"),
    ("Jukka's Daughter", "Annika"),
    ("Jukka's Daughter's Mom", "Katy"),
]

# Pseudo-entities + collective references — info on attached states is malformed
# anyway. Delete; orphan_scan will sweep up the now-edgeless State nodes later.
DELETE_LABELS = [
    # Pseudo-entities from referring-expression promotion
    "Earl Greyhound Scene",
    "Jello Chocolate Swirl Pudding Cups",
    "Chicken Legs",
    "Premade Mashed Potatoes",
    "Annika's Hair",
    "Jukka's desk",
    "Water Bottle",
    "Jukka's Group",
    "Sun",
    "Current Environment",
    "Cozy couch photo",
    "Chicken Case",
    "Original message",
    "Game",
    "Harvard",
    "Jukka's Grandpa's Passengers",
    # Collective placeholders
    "Jukka and Katy's children",
    "Kids",
    "Jukka's Kids",
    "Jukka's Children",
    "Other Children",
    "Jukka's Dogs",
    "Dogs",
    "Pet",
]

# Tables that hold a single node_id FK to kg_node_metadata. For DELETE we
# remove rows; for MERGE the merge function rebinds them. Mirrors the
# NODE_ID_REFERENCES list in node_merge.py.
NODE_REF_TABLES = [
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
    ("entity_card_v2", "entity_node_id"),
]


def _resolve_id(cur, label):
    rows = cur.execute(
        "SELECT id FROM kg_node_metadata WHERE label = ? AND node_type = 'Entity'",
        (label,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        # Multiple matches — pick the one with most edges
        best = max(rows, key=lambda r: cur.execute(
            "SELECT COUNT(*) FROM kg_edge_metadata WHERE source_id=? OR target_id=?",
            (r[0], r[0]),
        ).fetchone()[0])
        return best[0]
    return rows[0][0]


def _table_exists(cur, name):
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_exists(cur, table, col):
    if not _table_exists(cur, table):
        return False
    return any(r[1] == col for r in cur.execute(f"PRAGMA table_info({table})"))


def audit(con):
    import sqlite3
    cur = con.cursor()
    print("=== MERGE PLAN ===\n")
    for loser_lbl, winner_lbl in MERGE_ACTIONS:
        lid = _resolve_id(cur, loser_lbl)
        wid = _resolve_id(cur, winner_lbl)
        if lid is None:
            print(f"  SKIP merge: loser {loser_lbl!r} not found")
            continue
        if wid is None:
            print(f"  SKIP merge: winner {winner_lbl!r} not found")
            continue
        edges = cur.execute("SELECT COUNT(*) FROM kg_edge_metadata WHERE source_id=? OR target_id=?", (lid, lid)).fetchone()[0]
        findings = cur.execute("SELECT COUNT(*) FROM kg_maintenance_finding WHERE primary_node_id=? OR secondary_node_id=?", (lid, lid)).fetchone()[0]
        print(f"  MERGE {loser_lbl!r} ({lid[:8]}) → {winner_lbl!r} ({wid[:8]})")
        print(f"        edges to reroute: {edges}  findings to mark executed: {findings}")

    print()
    print("=== DELETE PLAN ===\n")
    for lbl in DELETE_LABELS:
        nid = _resolve_id(cur, lbl)
        if nid is None:
            print(f"  SKIP delete: {lbl!r} not found")
            continue
        edges = cur.execute("SELECT COUNT(*) FROM kg_edge_metadata WHERE source_id=? OR target_id=?", (nid, nid)).fetchone()[0]
        findings = cur.execute("SELECT COUNT(*) FROM kg_maintenance_finding WHERE primary_node_id=? OR secondary_node_id=?", (nid, nid)).fetchone()[0]
        print(f"  DELETE {lbl!r} ({nid[:8]})  edges: {edges}  findings: {findings}")


def commit(con):
    """Execute the cleanup. Bootstraps DI for the merge codepath."""
    import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI
    from app.models.base import get_session
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.assistant.kg_core.kg_utils.node_merge import merge_nodes_in_session

    cur = con.cursor()
    merged_n = 0
    deleted_n = 0
    finding_executed_n = 0
    finding_dismissed_n = 0

    # ── MERGES ──────────────────────────────────────────────────────────────
    for loser_lbl, winner_lbl in MERGE_ACTIONS:
        lid = _resolve_id(cur, loser_lbl)
        wid = _resolve_id(cur, winner_lbl)
        if lid is None or wid is None:
            print(f"  SKIP merge {loser_lbl!r}: not found")
            continue
        print(f"  MERGE {loser_lbl!r} → {winner_lbl!r}")

        # Mark related findings as executed BEFORE merge (so the loser_id is
        # still valid for the UPDATE)
        cur.execute(
            "UPDATE kg_maintenance_finding "
            "SET status='executed', executed_at=CURRENT_TIMESTAMP, "
            "    execution_notes=:n "
            "WHERE (primary_node_id=:lid OR secondary_node_id=:lid) "
            "  AND status='pending'",
            {"lid": lid, "n": f"auto-cleaned by ghost-entity audit (merged into {winner_lbl})"},
        )
        finding_executed_n += cur.rowcount
        con.commit()

        # Now do the merge through the proper service path
        session = get_session()
        try:
            loser = session.query(Node).filter(Node.id == lid).one_or_none()
            winner = session.query(Node).filter(Node.id == wid).one_or_none()
            if loser is None or winner is None:
                print(f"    ✗ node lookup failed for merge")
                session.close()
                continue
            merge_nodes_in_session(
                session,
                loser_node=loser,
                winner_node=winner,
                merge_actor="ghost_entity_cleanup_2026_05_11",
                notes=f"audit found {loser_lbl!r} was a kinship-phrase ghost duplicate of {winner_lbl!r}",
            )
            session.commit()
            print(f"    ✓ merged")
            merged_n += 1
        except Exception as e:
            session.rollback()
            print(f"    ✗ merge failed: {type(e).__name__}: {e}")
        finally:
            session.close()

    # ── DELETES ─────────────────────────────────────────────────────────────
    cur.execute("BEGIN")
    try:
        for lbl in DELETE_LABELS:
            nid = _resolve_id(cur, lbl)
            if nid is None:
                print(f"  SKIP delete {lbl!r}: not found")
                continue
            print(f"  DELETE {lbl!r}")

            # Mark related findings as dismissed
            cur.execute(
                "UPDATE kg_maintenance_finding "
                "SET status='dismissed', executed_at=CURRENT_TIMESTAMP, "
                "    execution_notes=:n "
                "WHERE (primary_node_id=:nid OR secondary_node_id=:nid) "
                "  AND status='pending'",
                {"nid": nid, "n": f"auto-cleaned: node {lbl!r} was a pseudo-entity, deleted"},
            )
            finding_dismissed_n += cur.rowcount

            # Edges first (FK CASCADE was dropped 2026-05-10)
            cur.execute("DELETE FROM kg_edge_metadata WHERE source_id=:n OR target_id=:n", {"n": nid})

            # Dependent referencing tables
            for tbl, col in NODE_REF_TABLES:
                if _column_exists(cur, tbl, col):
                    cur.execute(f"DELETE FROM {tbl} WHERE {col}=?", (nid,))

            # The node itself
            cur.execute("DELETE FROM kg_node_metadata WHERE id=?", (nid,))
            deleted_n += 1
        con.commit()
    except Exception:
        con.rollback()
        raise

    print()
    print(f"=== summary ===")
    print(f"  merged: {merged_n}")
    print(f"  deleted: {deleted_n}")
    print(f"  findings marked executed: {finding_executed_n}")
    print(f"  findings marked dismissed: {finding_dismissed_n}")


def main():
    import sqlite3
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--commit", action="store_true")
    p.add_argument("--yes-write", action="store_true")
    args = p.parse_args()
    if not (args.audit or args.commit):
        args.audit = True

    con = sqlite3.connect(DB_PATH)
    try:
        if args.audit:
            audit(con)
            return
        if not args.yes_write:
            sys.exit("REFUSED: pass --yes-write to actually mutate emi.db")
        if not os.path.exists(BACKUP_PATH):
            sys.exit(f"REFUSED: no backup at {BACKUP_PATH}")
        age = time.time() - os.path.getmtime(BACKUP_PATH)
        if age > BACKUP_FRESH_SECONDS:
            sys.exit(f"REFUSED: emi.db.bak is {age/60:.0f} min old (must be < 60)")
        print(f"backup OK ({age:.0f}s old)\n")
        commit(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
