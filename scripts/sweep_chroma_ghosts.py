"""sweep_chroma_ghosts.py — one-time cleanup of Chroma ghost vectors.

Merges performed via merge_nodes_in_session before 2026-06-10 (the
duplicate_cluster_drain backlog clear: ~293 merges, plus the daily 03:15
drain since 2026-06-09) deleted loser rows from kg_node_metadata without
removing their label/context embeddings from Chroma. The duplicate scan's
tier-3 similarity lookup reads raw collection ids, so those ghost vectors
feed it false merge candidates daily. Fixed at the source on 2026-06-10
(node_merge.merge_nodes_in_session now deletes the loser's vectors);
this script sweeps the ghosts already minted — by any historical path.

Scans three collections against the live SQLite tables:
  - node_embeddings          vs kg_node_metadata.id
  - node_context_embeddings  vs kg_node_metadata.id
  - edge_embeddings          vs kg_edge_metadata.id   (dropped-duplicate
    edges from merges never had Chroma cleanup either)

Modes:
  --audit   (default) Print ghost counts + samples. No writes.
  --commit  Delete the ghost vectors. Stop the EmiOS server first — two
            PersistentClients on one chroma dir is unsupported.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
DB_PATH = REPO / "emi.db"

PAGE = 5000
DELETE_BATCH = 500

# (collection_name, sqlite_table) — collection ids are keyed by the table's id.
SWEEPS = [
    ("node_embeddings", "kg_node_metadata"),
    ("node_context_embeddings", "kg_node_metadata"),
    ("edge_embeddings", "kg_edge_metadata"),
]


def _live_ids(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[0]) for r in con.execute(f"SELECT id FROM {table}")}


def _all_collection_ids(col) -> list[str]:
    ids: list[str] = []
    offset = 0
    while True:
        batch = col.get(limit=PAGE, offset=offset, include=[])
        got = batch.get("ids") or []
        ids.extend(str(i) for i in got)
        if len(got) < PAGE:
            return ids
        offset += PAGE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true", help="report only (default)")
    mode.add_argument("--commit", action="store_true", help="delete ghost vectors")
    args = parser.parse_args()
    commit = bool(args.commit)

    if not DB_PATH.exists():
        raise SystemExit(f"SQLite DB not found at {DB_PATH}")

    import chromadb
    from chromadb.config import Settings

    from app.assistant.utils.path_utils import get_chroma_kg_db_dir

    chroma_dir = get_chroma_kg_db_dir()
    if not chroma_dir.exists():
        raise SystemExit(f"Chroma dir not found at {chroma_dir}")

    client = chromadb.PersistentClient(
        path=str(chroma_dir),
        settings=Settings(anonymized_telemetry=False),
    )
    con = sqlite3.connect(str(DB_PATH))

    print(f"DB:     {DB_PATH}")
    print(f"Chroma: {chroma_dir}")
    print(f"Mode:   {'COMMIT' if commit else 'audit (read-only)'}\n")

    total_ghosts = 0
    try:
        for col_name, table in SWEEPS:
            col = client.get_collection(col_name)
            live = _live_ids(con, table)
            vec_ids = _all_collection_ids(col)
            ghosts = [i for i in vec_ids if i not in live]
            total_ghosts += len(ghosts)

            print(f"{col_name}: {len(vec_ids)} vectors, {len(live)} live {table} rows "
                  f"→ {len(ghosts)} ghost(s)")
            for sample in ghosts[:5]:
                print(f"    ghost id: {sample}")
            if len(ghosts) > 5:
                print(f"    … and {len(ghosts) - 5} more")

            if commit and ghosts:
                for i in range(0, len(ghosts), DELETE_BATCH):
                    col.delete(ids=ghosts[i:i + DELETE_BATCH])
                print(f"    DELETED {len(ghosts)} ghost vector(s)")
            print()
    finally:
        con.close()

    if commit:
        print(f"Done. {total_ghosts} ghost vector(s) removed.")
    else:
        print(f"Audit only — {total_ghosts} ghost vector(s) found. "
              f"Re-run with --commit (server stopped) to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
