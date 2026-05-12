"""Audit kg attributes JSON columns for duplication with first-class columns.

Walks `kg_node_metadata.attributes`, `kg_edge_metadata.attributes`, and
`claim_proposal_node.attributes_json`. For each, reports the distribution of
keys and flags any key that has a corresponding first-class column on the
same table — those are bugs (data stored in two places).

Run periodically (or in CI) to catch regressions: any time someone adds a
new field to an LLM agent's output, this script reveals whether the
downstream pop-to-first-class pattern was wired up correctly.

Usage:
    python scripts/audit_kg_attributes_json.py
    python scripts/audit_kg_attributes_json.py --strict   # exit 1 if any dup found

Output:
    Per-key counts grouped by table, with DUP/orphan flags. Sample rows for
    any orphan keys (singletons that look like extractor artifacts).

Exit code:
    0 if no duplicates (or --strict not set); 1 if --strict and dups found.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

DB_PATH = Path(__file__).resolve().parent.parent / "emi.db"

# Tables to audit: (table_name, json_column, related_first_class_table)
# When `related_first_class_table` differs from `table_name`, the duplicate
# check uses the related table's columns — this is the case for
# `claim_proposal_node.attributes_json` (proposal layer) where the dup we
# care about is against `kg_node_metadata`'s first-class columns (since the
# promoter pops fields named the same as Node columns).
AUDIT_TARGETS: List[Tuple[str, str, str]] = [
    ("kg_node_metadata", "attributes", "kg_node_metadata"),
    ("kg_edge_metadata", "attributes", "kg_edge_metadata"),
    ("claim_proposal_node", "attributes_json", "kg_node_metadata"),
]


def first_class_columns(con: sqlite3.Connection, table: str) -> Set[str]:
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    # Exclude the JSON column itself
    cols -= {"attributes", "attributes_json"}
    return cols


def audit_table(
    con: sqlite3.Connection,
    table: str,
    json_col: str,
    related_table: str,
) -> Tuple[Dict[str, int], Set[str]]:
    """Scan one table's JSON column. Return (key_counts, duplicates)."""
    sql = (
        f"SELECT {json_col} FROM {table} "
        f"WHERE {json_col} IS NOT NULL AND {json_col} != '{{}}'"
    )
    key_counts: Dict[str, int] = {}
    for (raw,) in con.execute(sql).fetchall():
        try:
            d = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        for k in d.keys():
            key_counts[k] = key_counts.get(k, 0) + 1
    firstclass = first_class_columns(con, related_table)
    dups = {k for k in key_counts if k in firstclass}
    return key_counts, dups


def sample_orphan(
    con: sqlite3.Connection, table: str, json_col: str, key: str, limit: int = 3,
) -> List[Tuple[str, str]]:
    """Return up to `limit` (id, raw_json) samples for rows containing `key`."""
    sql = (
        f"SELECT id, {json_col} FROM {table} "
        f"WHERE {json_col} LIKE ? LIMIT ?"
    )
    rows = con.execute(sql, (f'%"{key}"%', limit)).fetchall()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any duplicate keys are found (for CI).",
    )
    parser.add_argument(
        "--orphan-threshold",
        type=int,
        default=5,
        help="Keys appearing in fewer than N rows are flagged as orphans (default: 5).",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        return 2

    con = sqlite3.connect(DB_PATH)
    any_dups = False
    any_orphans = False

    for table, json_col, related in AUDIT_TARGETS:
        try:
            key_counts, dups = audit_table(con, table, json_col, related)
        except sqlite3.OperationalError as e:
            print(f"[skip] {table}.{json_col}: {e}")
            continue
        if not key_counts:
            continue
        print(f"\n=== {table}.{json_col}"
              f"{f' (dup-check vs {related})' if related != table else ''} ===")
        firstclass = first_class_columns(con, related)
        for k, c in sorted(key_counts.items(), key=lambda x: -x[1]):
            tag = ""
            if k in firstclass:
                tag = " <<< DUP (also a first-class column)"
                any_dups = True
            elif c < args.orphan_threshold:
                tag = " <<< ORPHAN (rare — probably extractor artifact)"
                any_orphans = True
            print(f"  {k:30} {c:>6}{tag}")

        # Sample orphan rows
        orphan_keys = [k for k, c in key_counts.items() if c < args.orphan_threshold]
        for k in orphan_keys:
            samples = sample_orphan(con, table, json_col, k)
            for (rid, raw) in samples:
                preview = (raw[:120] + "...") if len(raw) > 120 else raw
                print(f"    [{k}] {rid[:8]}: {preview}")

    con.close()

    print("\n" + ("=" * 60))
    if any_dups:
        print("DUPLICATES FOUND — fields stored in BOTH JSON and a first-class column.")
        print("Fix: pop the field at the writer's promotion step (see proposal_promoter._create_kg_node_from_proposal for the pattern).")
    if any_orphans:
        print("ORPHAN KEYS FOUND — singletons that look like extractor artifacts.")
        print("Usually safe to ignore unless they accumulate. Investigate the extractor / enricher prompt if they grow.")
    if not any_dups and not any_orphans:
        print("Clean — no duplicates, no orphan keys above threshold.")

    if args.strict and any_dups:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
