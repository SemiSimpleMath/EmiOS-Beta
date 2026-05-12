"""migrate_entity_card_v2_nullable_node_id.py — entity_card_v2 phase 1 migration.

Three changes to `entity_card_v2`:

  1) entity_node_id: NOT NULL → nullable
     (so user-authored non-kg cards can live in the same table, distinguished
     by entity_node_id IS NULL)

  2) Add card_title TEXT — denormalized lookup field. For kg cards the
     builder populates it from kg_node_metadata.label. For non-kg cards
     the user provides it.

  3) Add card_aliases TEXT (JSON) — denormalized aliases list. Same
     story: builder copies from kg_node_metadata.aliases for kg cards;
     user provides for non-kg cards.

After these, retrieval (EntityCatalog + render_v2_card_for_prompt_injection_level)
scans entity_card_v2 directly instead of going through the KG node.

SQLite cannot DROP NOT NULL in place, so this performs the standard
table-recreate dance:

    BEGIN;
    CREATE TABLE entity_card_v2__new (... relaxed schema + new cols);
    INSERT INTO entity_card_v2__new SELECT ... FROM entity_card_v2;
    DROP TABLE entity_card_v2;
    ALTER TABLE entity_card_v2__new RENAME TO entity_card_v2;
    -- recreate indexes
    -- backfill card_title / card_aliases from kg_node_metadata
    COMMIT;

Modes:
  --audit       Show current schema + row counts. No DB ops.
  --commit      Apply migration. Requires emi.db.bak < 1hr old + --yes-write.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
DB_PATH = str(REPO / "emi.db")
BACKUP_PATH = str(REPO / "emi.db.bak")
BACKUP_FRESH_SECONDS = 60 * 60


def get_column_info(con: sqlite3.Connection, table: str):
    return {r[1]: r for r in con.execute(f'PRAGMA table_info("{table}")')}


def audit(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    cols = get_column_info(con, "entity_card_v2")
    if not cols:
        print("  entity_card_v2: table not found — nothing to migrate")
        return
    rowcount = cur.execute("SELECT COUNT(*) FROM entity_card_v2").fetchone()[0]
    null_count = cur.execute(
        "SELECT COUNT(*) FROM entity_card_v2 WHERE entity_node_id IS NULL"
    ).fetchone()[0]
    print(f"  entity_card_v2: rows={rowcount}  non_kg(NULL entity_node_id)={null_count}")
    print("  column status:")
    for name in ("entity_node_id", "card_title", "card_aliases"):
        c = cols.get(name)
        if c is None:
            print(f"    {name}: MISSING — will add")
        else:
            # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
            print(f"    {name}: type={c[2]} notnull={bool(c[3])}")
    print()
    print("[audit] proposed actions:")
    if cols.get("entity_node_id") and cols["entity_node_id"][3]:
        print("  - drop NOT NULL on entity_node_id (via table-recreate)")
    else:
        print("  - entity_node_id NOT NULL already gone, skip recreate")
    for name in ("card_title", "card_aliases"):
        if name not in cols:
            print(f"  - add column {name}")
        else:
            print(f"  - {name} already present, skip ADD")
    print("  - backfill card_title from kg_node_metadata.label (WHERE entity_node_id IS NOT NULL AND card_title IS NULL)")
    print("  - backfill card_aliases from kg_node_metadata.aliases (same WHERE)")


def commit_migration(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    cols = get_column_info(con, "entity_card_v2")
    need_recreate = bool(cols.get("entity_node_id") and cols["entity_node_id"][3])
    need_card_title = "card_title" not in cols
    need_card_aliases = "card_aliases" not in cols

    if not (need_recreate or need_card_title or need_card_aliases):
        print("  schema already matches target — nothing structural to do")
    else:
        print(f"  need_recreate={need_recreate} need_card_title={need_card_title} need_card_aliases={need_card_aliases}")

    # Disable FK enforcement during the table swap; the child tables
    # (entity_card_section etc.) reference entity_card_v2.id which we
    # preserve in the new table.
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("BEGIN")
    try:
        if need_recreate:
            print("  recreating entity_card_v2 with relaxed NOT NULL + new columns ...")
            cur.execute("""
                CREATE TABLE entity_card_v2__new (
                    id VARCHAR PRIMARY KEY,
                    entity_node_id VARCHAR,
                    entity_type VARCHAR NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    last_built_at DATETIME,
                    last_full_rebuild_at DATETIME,
                    last_incremental_at DATETIME,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    card_title TEXT,
                    card_aliases TEXT,
                    FOREIGN KEY(entity_node_id) REFERENCES kg_node_metadata(id) ON DELETE CASCADE,
                    UNIQUE(entity_node_id)
                )
            """)
            # Copy existing rows; new columns default to NULL.
            cur.execute("""
                INSERT INTO entity_card_v2__new (
                    id, entity_node_id, entity_type, is_active,
                    last_built_at, last_full_rebuild_at, last_incremental_at,
                    created_at, updated_at, card_title, card_aliases
                )
                SELECT
                    id, entity_node_id, entity_type, is_active,
                    last_built_at, last_full_rebuild_at, last_incremental_at,
                    created_at, updated_at, NULL, NULL
                FROM entity_card_v2
            """)
            cur.execute("DROP TABLE entity_card_v2")
            cur.execute("ALTER TABLE entity_card_v2__new RENAME TO entity_card_v2")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_entity_card_v2_entity_node_id ON entity_card_v2(entity_node_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_entity_card_v2_active ON entity_card_v2(is_active)")
            cur.execute("CREATE INDEX IF NOT EXISTS ix_entity_card_v2_card_title ON entity_card_v2(card_title)")
            print("    ✓ table recreated with relaxed entity_node_id + card_title/card_aliases columns")
        else:
            # No recreate needed; just ADD COLUMN for missing pieces.
            if need_card_title:
                cur.execute("ALTER TABLE entity_card_v2 ADD COLUMN card_title TEXT")
                cur.execute("CREATE INDEX IF NOT EXISTS ix_entity_card_v2_card_title ON entity_card_v2(card_title)")
                print("    ✓ added card_title")
            if need_card_aliases:
                cur.execute("ALTER TABLE entity_card_v2 ADD COLUMN card_aliases TEXT")
                print("    ✓ added card_aliases")

        # Backfill: for kg-backed rows, copy node.label → card_title and
        # node.aliases → card_aliases. Only fill where currently NULL so
        # re-running is idempotent.
        print("  backfilling card_title from kg_node_metadata.label ...")
        cur.execute("""
            UPDATE entity_card_v2 AS c
            SET card_title = (
                SELECT n.label FROM kg_node_metadata n WHERE n.id = c.entity_node_id
            )
            WHERE c.entity_node_id IS NOT NULL AND c.card_title IS NULL
        """)
        print(f"    ✓ rows touched: {cur.rowcount}")

        print("  backfilling card_aliases from kg_node_metadata.aliases ...")
        # Read rows that still have NULL card_aliases + a node behind them.
        # We re-emit the aliases JSON as TEXT — sqlite stores it the same way.
        rows = cur.execute("""
            SELECT c.id, n.aliases
            FROM entity_card_v2 c
            JOIN kg_node_metadata n ON n.id = c.entity_node_id
            WHERE c.card_aliases IS NULL
        """).fetchall()
        for card_id, aliases_json in rows:
            if aliases_json is None:
                continue
            # node.aliases is stored as JSON text; pass through verbatim.
            cur.execute(
                "UPDATE entity_card_v2 SET card_aliases = ? WHERE id = ?",
                (aliases_json, card_id),
            )
        print(f"    ✓ rows backfilled: {len(rows)}")

        con.commit()
        print("=== migration committed ===")
    except Exception as exc:
        con.rollback()
        print(f"FAILED: {type(exc).__name__}: {exc}")
        raise
    finally:
        cur.execute("PRAGMA foreign_keys = ON")

    print()
    print("Post-migration verification:")
    cols = get_column_info(con, "entity_card_v2")
    for name in ("entity_node_id", "card_title", "card_aliases"):
        c = cols.get(name)
        if c is None:
            print(f"  {name}: MISSING (migration failed)")
        else:
            print(f"  {name}: type={c[2]} notnull={bool(c[3])}")
    rowcount = cur.execute("SELECT COUNT(*) FROM entity_card_v2").fetchone()[0]
    titled = cur.execute("SELECT COUNT(*) FROM entity_card_v2 WHERE card_title IS NOT NULL").fetchone()[0]
    aliased = cur.execute("SELECT COUNT(*) FROM entity_card_v2 WHERE card_aliases IS NOT NULL").fetchone()[0]
    print(f"  rows={rowcount}  with_card_title={titled}  with_card_aliases={aliased}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--commit", action="store_true")
    p.add_argument("--yes-write", action="store_true",
                   help="Required to actually mutate emi.db in --commit mode")
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
        print(f"backup OK ({age:.0f}s old)")
        commit_migration(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
