"""cleanup_extractions_2026_01_to_03.py — wipe stale extractions for the
2026-01-01 → 2026-03-01 LOCAL range so the (fixed) resolver's output drives
a fresh extract → promote pass.

For windows in range:
  - delete kg_window_enrichment rows (FK to window_extraction.id)
  - delete kg_window_extraction rows
  - delete claim_proposal_node / _edge / _evidence for proposals whose evidence
    is tied to these windows (all currently 'abandoned')
  - delete claim_proposal rows themselves

kg_window + kg_window_message rows are PRESERVED (we trust the segmenter's
boundaries — only the extractor/canonicalizer benefits from the resolver fix
via the JOIN to kg_resolved_message).

Modes:
  --audit       Counts only, no mutations.
  --commit      Apply. Requires --yes-write + emi.db.bak < 1h.
"""
from __future__ import annotations

import argparse
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

# Local 2026-01-01 → 2026-03-02 in PST/PDT ≈ UTC offset 8h (winter PST):
#   start_utc = 2026-01-01T08:00:00 UTC
#   end_utc   = 2026-03-02T08:00:00 UTC
START_UTC = "2026-01-01 08:00:00"
END_UTC   = "2026-03-02 08:00:00"


def _collect(cur):
    windows = [r[0] for r in cur.execute(
        "SELECT id FROM kg_window WHERE start_timestamp >= ? AND start_timestamp < ?",
        (START_UTC, END_UTC),
    ).fetchall()]
    if not windows:
        return [], [], []
    placeholders = ",".join("?" * len(windows))
    proposals = [r[0] for r in cur.execute(
        f"SELECT DISTINCT proposal_id FROM claim_proposal_evidence WHERE window_id IN ({placeholders})",
        windows,
    ).fetchall()]
    extractions = [r[0] for r in cur.execute(
        f"SELECT id FROM kg_window_extraction WHERE window_id IN ({placeholders})",
        windows,
    ).fetchall()]
    return windows, proposals, extractions


def audit(con):
    cur = con.cursor()
    windows, proposals, extractions = _collect(cur)
    print(f"target windows: {len(windows)}")
    print(f"target proposals: {len(proposals)}  (status breakdown:")
    if proposals:
        ph = ",".join("?" * len(proposals))
        for st, n in cur.execute(f"SELECT status, COUNT(*) FROM claim_proposal WHERE id IN ({ph}) GROUP BY status", proposals).fetchall():
            print(f"    {st!r}: {n}")
    print(f"  )")
    print(f"target kg_window_extraction rows: {len(extractions)}")

    if proposals:
        ph = ",".join("?" * len(proposals))
        for tbl in ("claim_proposal_node", "claim_proposal_edge", "claim_proposal_evidence"):
            n = cur.execute(f"SELECT COUNT(*) FROM {tbl} WHERE proposal_id IN ({ph})", proposals).fetchone()[0]
            print(f"  {tbl}: {n}")
    if extractions:
        ph = ",".join("?" * len(extractions))
        n = cur.execute(f"SELECT COUNT(*) FROM kg_window_enrichment WHERE window_extraction_id IN ({ph})", extractions).fetchone()[0]
        print(f"  kg_window_enrichment: {n}")


def commit(con):
    cur = con.cursor()
    windows, proposals, extractions = _collect(cur)
    cur.execute("BEGIN")
    deleted = {}
    try:
        if extractions:
            ph = ",".join("?" * len(extractions))
            n = cur.execute(f"DELETE FROM kg_window_enrichment WHERE window_extraction_id IN ({ph})", extractions).rowcount
            deleted["kg_window_enrichment"] = n
        if proposals:
            ph = ",".join("?" * len(proposals))
            for tbl in ("claim_proposal_node", "claim_proposal_edge", "claim_proposal_evidence"):
                n = cur.execute(f"DELETE FROM {tbl} WHERE proposal_id IN ({ph})", proposals).rowcount
                deleted[tbl] = n
            n = cur.execute(f"DELETE FROM claim_proposal WHERE id IN ({ph})", proposals).rowcount
            deleted["claim_proposal"] = n
        if windows:
            ph = ",".join("?" * len(windows))
            n = cur.execute(f"DELETE FROM kg_window_extraction WHERE window_id IN ({ph})", windows).rowcount
            deleted["kg_window_extraction"] = n
        con.commit()
        print("=== committed ===")
        for k, v in deleted.items():
            print(f"  deleted {v} rows from {k}")
    except Exception:
        con.rollback()
        raise


def main():
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
        audit(con)
        print()
        commit(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
