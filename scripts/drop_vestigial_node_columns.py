"""
Drop the 3 vestigial denormalized provenance columns from kg_node_metadata:
  - window_id              (data is in kg_node_evidence + kg_window)
  - original_message_id    (mostly NULL post-rebuild; the 489 ancient nodes
                            holding this column have dead pointers — 0% resolve
                            in unified_log_2026)
  - sentence_id            (points at retired kg_chat_parsed_sentence table)

Background
----------
Per proposal_promoter.py:769 — "Verbatim source is preserved in evidence +
window_id, not on the node." Since the 2026-04-26 KG-pipeline rebuild, these
columns are intentionally NULL on every newly-created node. The kg_node_viewer
template doesn't render them; only the route's dict emits them as dead keys.

Verification before removal:
  - Of the 3,675 nodes with any column populated, 3,186 also have
    kg_node_evidence rows (safe — data is duplicated).
  - The 489 that don't all have DEAD pointers:
      * window_id: 0 of them have unique data (all in kg_window).
      * original_message_id: 0% resolve in unified_log_2026 (broken IDs).
      * sentence_id: literally the string "sentence" (placeholder bug);
        kg_chat_parsed_sentence table has been retired.

So dropping these columns loses zero useful data.

Usage
-----
Dry-run (default) prints planned operations:

    .venv/Scripts/python.exe scripts/drop_vestigial_node_columns.py

Commit:

    .venv/Scripts/python.exe scripts/drop_vestigial_node_columns.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


COLUMNS_TO_DROP = ["window_id", "original_message_id", "sentence_id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="Persist the DROP COLUMN statements.")
    args = parser.parse_args()

    from app.models.db_manager import get_db_manager
    from sqlalchemy import text

    print(f"=== DROP COLUMNS — {'COMMIT' if args.commit else 'DRY-RUN'} ===\n")

    # Read current schema for reporting.
    with get_db_manager().read_session() as s:
        cols = s.execute(text("PRAGMA table_info(kg_node_metadata)")).fetchall()
        col_names = {c[1] for c in cols}
        print(f"Current kg_node_metadata columns: {len(col_names)}")
        for c in COLUMNS_TO_DROP:
            print(f"  {c}: {'PRESENT' if c in col_names else 'already dropped'}")

    if not args.commit:
        print("\n(dry-run — no DDL changes)")
        return 0

    # SQLite ALTER TABLE DROP COLUMN exists since 3.35; we verified 3.40 in use.
    # Drop any indexes that reference these columns first — SQLite refuses
    # to drop a column while an index references it.
    with get_db_manager().transaction(op="drop_vestigial_node_columns") as session:
        for col in COLUMNS_TO_DROP:
            if col not in col_names:
                continue
            idx_rows = session.execute(text(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='kg_node_metadata' "
                "AND sql LIKE :pat"
            ), {"pat": f"%({col})%"}).fetchall()
            for (idx_name,) in idx_rows:
                stmt = f"DROP INDEX IF EXISTS {idx_name}"
                print(f"  EXEC: {stmt}")
                session.execute(text(stmt))
            stmt = f"ALTER TABLE kg_node_metadata DROP COLUMN {col}"
            print(f"  EXEC: {stmt}")
            session.execute(text(stmt))

    # Verify post.
    with get_db_manager().read_session() as s:
        cols = s.execute(text("PRAGMA table_info(kg_node_metadata)")).fetchall()
        col_names = {c[1] for c in cols}
        print(f"\nPost-drop kg_node_metadata columns: {len(col_names)}")
        for c in COLUMNS_TO_DROP:
            print(f"  {c}: {'still present (FAILED)' if c in col_names else 'gone'}")

    print("\n=== DONE ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
