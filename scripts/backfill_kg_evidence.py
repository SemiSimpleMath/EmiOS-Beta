"""
Backfill kg_node_evidence + kg_edge_evidence rows for the post-rebuild cohort.

Background
----------
Commit 27530c59 (2026-04-26) rebuilt the KG pipeline into the bucket-per-stage
architecture. The rebuild moved node creation into proposal_promoter but did
not migrate the evidence-write step that lived in the legacy
kg_chat_pipeline_parallel/utils/merge_utils.py:write_node_evidence helper.

Effect: every node and most edges promoted between 2026-04-26 and 2026-05-04
have zero kg_node_evidence / kg_edge_evidence rows. The kg_node_viewer's
provenance UI shows blank for them, and the node_merger LLM gets less context
than designed when deciding State/Event matches.

The forward fix (committed alongside this script) restored the writers so
all future promotions write evidence at all 6 commit sites. This script
backfills the affected cohort.

Source data
-----------
For each post-rebuild claim_proposal_node with resolved_node_id set, the
parent proposal's claim_proposal_evidence row carries every field needed
to reconstruct kg_node_evidence (window_id, unified_log_id, raw_text,
observed_at). Same for claim_proposal_edge → kg_edge_evidence.

We look up each promoted proposal_node / proposal_edge that has a
resolved_node_id / resolved_edge_id, check that no evidence row already
exists for it, then insert one with merge_action derived from the proposal
node's resolution_action ("created_new" → "created", "matched_existing" →
"confirmed"). Edges have no resolution_action column; we infer
created-vs-matched by checking whether the kg_edge_metadata row's
created_from_proposal_id matches THIS proposal (created) or not (matched).

Idempotency
-----------
Before each insert we check: does a kg_node_evidence row with
(node_id, window_id, source_id=unified_log_id) already exist? If yes, skip.
That keys on what would otherwise be unique per-observation.

Usage
-----
Dry-run (default) prints insert counts without writing:

    .venv/Scripts/python.exe scripts/backfill_kg_evidence.py

Commit:

    .venv/Scripts/python.exe scripts/backfill_kg_evidence.py --commit
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from typing import Optional

# The script imports DI-using modules but does not need full DI bootstrap.
# Just make sure project root is on sys.path so absolute imports work.
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


REBUILD_CUTOFF_ISO = "2026-04-26"  # commit 27530c59 landed Apr 26 17:11 PDT


def _action_for_resolution(resolution_action: str) -> str:
    """Map claim_proposal_node.resolution_action → kg_node_evidence.merge_action."""
    if resolution_action == "created_new":
        return "created"
    if resolution_action == "matched_existing":
        return "confirmed"
    # held_needs_existing / skipped_locked / pending — no node evidence to write.
    return ""


def _backfill_node_evidence(session, *, commit: bool, verbose: bool) -> dict:
    """Walk promoted post-rebuild proposal_nodes; insert missing
    kg_node_evidence rows.

    Returns counter dict.
    """
    from sqlalchemy import text

    counters = {
        "scanned": 0,
        "skipped_no_resolution": 0,
        "skipped_already_present": 0,
        "skipped_no_evidence": 0,
        "would_insert": 0,
        "inserted": 0,
        "by_action": {},
    }

    rows = session.execute(text(f"""
        SELECT
            cpn.proposal_id        AS proposal_id,
            cpn.resolved_node_id   AS node_id,
            cpn.resolution_action  AS resolution_action,
            cpn.sentence           AS derived_sentence
        FROM claim_proposal_node cpn
        JOIN claim_proposal cp ON cp.id = cpn.proposal_id
        WHERE cp.status = 'promoted'
          AND cp.created_at >= '{REBUILD_CUTOFF_ISO}'
          AND cpn.resolved_node_id IS NOT NULL
        ORDER BY cp.created_at ASC
    """)).fetchall()

    for r in rows:
        counters["scanned"] += 1
        proposal_id = r[0]
        node_id = r[1]
        resolution_action = r[2] or ""
        derived_sentence = r[3]

        merge_action = _action_for_resolution(resolution_action)
        if not merge_action:
            counters["skipped_no_resolution"] += 1
            continue

        # Pull the proposal's earliest evidence row for the source provenance.
        ev = session.execute(text("""
            SELECT id, window_id, unified_log_id, raw_text, observed_at
            FROM claim_proposal_evidence
            WHERE proposal_id = :pid
            ORDER BY observed_at ASC
            LIMIT 1
        """), {"pid": proposal_id}).fetchone()
        if ev is None:
            counters["skipped_no_evidence"] += 1
            continue

        window_id = ev[1]
        unified_log_id = ev[2]
        raw_text = ev[3]
        observed_at = ev[4]

        # Idempotency: skip if a kg_node_evidence row already exists for
        # this node + window + source_id triple.
        already = session.execute(text("""
            SELECT 1 FROM kg_node_evidence
            WHERE node_id = :nid
              AND COALESCE(window_id, '')   = COALESCE(:win, '')
              AND COALESCE(source_id, '')   = COALESCE(:sid, '')
            LIMIT 1
        """), {
            "nid": node_id,
            "win": window_id,
            "sid": unified_log_id,
        }).fetchone()
        if already:
            counters["skipped_already_present"] += 1
            continue

        counters["by_action"][merge_action] = counters["by_action"].get(merge_action, 0) + 1
        counters["would_insert"] += 1
        if verbose and counters["would_insert"] <= 10:
            print(f"  + node={node_id[:8]} action={merge_action} window={window_id[:8] if window_id else '-'}")

        if commit:
            session.execute(text("""
                INSERT INTO kg_node_evidence (
                    id, node_id, source_table, source_id, source_text,
                    derived_sentence, message_timestamp, window_id, merge_action,
                    created_at
                ) VALUES (
                    :id, :nid, :stbl, :sid, :stxt,
                    :dsent, :mts, :win, :ma,
                    :now
                )
            """), {
                "id": str(uuid.uuid4()),
                "nid": node_id,
                "stbl": "unified_log_2026" if unified_log_id else None,
                "sid": unified_log_id,
                "stxt": raw_text,
                "dsent": derived_sentence,
                "mts": observed_at,
                "win": window_id,
                "ma": merge_action,
                "now": datetime.utcnow(),
            })
            counters["inserted"] += 1

    return counters


def _backfill_edge_evidence(session, *, commit: bool, verbose: bool) -> dict:
    """Walk promoted post-rebuild proposal_edges with resolved_edge_id; insert
    missing kg_edge_evidence rows. merge_action is "created" if the live edge
    was made by THIS proposal (created_from_proposal_id matches), else "confirmed".
    """
    from sqlalchemy import text

    counters = {
        "scanned": 0,
        "skipped_no_resolution": 0,
        "skipped_already_present": 0,
        "skipped_no_evidence": 0,
        "would_insert": 0,
        "inserted": 0,
        "by_action": {},
    }

    rows = session.execute(text(f"""
        SELECT
            cpe.proposal_id        AS proposal_id,
            cpe.resolved_edge_id   AS edge_id,
            cpe.sentence           AS derived_sentence,
            ekg.created_from_proposal_id AS edge_origin_proposal_id
        FROM claim_proposal_edge cpe
        JOIN claim_proposal cp ON cp.id = cpe.proposal_id
        LEFT JOIN kg_edge_metadata ekg ON ekg.id = cpe.resolved_edge_id
        WHERE cp.status = 'promoted'
          AND cp.created_at >= '{REBUILD_CUTOFF_ISO}'
          AND cpe.resolved_edge_id IS NOT NULL
        ORDER BY cp.created_at ASC
    """)).fetchall()

    for r in rows:
        counters["scanned"] += 1
        proposal_id = r[0]
        edge_id = r[1]
        derived_sentence = r[2]
        edge_origin_proposal_id = r[3]

        merge_action = "created" if edge_origin_proposal_id == proposal_id else "confirmed"

        ev = session.execute(text("""
            SELECT id, window_id, unified_log_id, raw_text, observed_at
            FROM claim_proposal_evidence
            WHERE proposal_id = :pid
            ORDER BY observed_at ASC
            LIMIT 1
        """), {"pid": proposal_id}).fetchone()
        if ev is None:
            counters["skipped_no_evidence"] += 1
            continue

        window_id = ev[1]
        unified_log_id = ev[2]
        raw_text = ev[3]
        observed_at = ev[4]

        already = session.execute(text("""
            SELECT 1 FROM kg_edge_evidence
            WHERE edge_id = :eid
              AND COALESCE(window_id, '')   = COALESCE(:win, '')
              AND COALESCE(source_id, '')   = COALESCE(:sid, '')
            LIMIT 1
        """), {
            "eid": edge_id,
            "win": window_id,
            "sid": unified_log_id,
        }).fetchone()
        if already:
            counters["skipped_already_present"] += 1
            continue

        counters["by_action"][merge_action] = counters["by_action"].get(merge_action, 0) + 1
        counters["would_insert"] += 1
        if verbose and counters["would_insert"] <= 10:
            print(f"  + edge={edge_id[:8]} action={merge_action} window={window_id[:8] if window_id else '-'}")

        if commit:
            session.execute(text("""
                INSERT INTO kg_edge_evidence (
                    id, edge_id, source_table, source_id, source_text,
                    derived_sentence, message_timestamp, window_id, merge_action,
                    created_at
                ) VALUES (
                    :id, :eid, :stbl, :sid, :stxt,
                    :dsent, :mts, :win, :ma,
                    :now
                )
            """), {
                "id": str(uuid.uuid4()),
                "eid": edge_id,
                "stbl": "unified_log_2026" if unified_log_id else None,
                "sid": unified_log_id,
                "stxt": raw_text,
                "dsent": derived_sentence,
                "mts": observed_at,
                "win": window_id,
                "ma": merge_action,
                "now": datetime.utcnow(),
            })
            counters["inserted"] += 1

    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit", action="store_true",
        help="Persist inserts. Without this, runs in dry-run mode and prints counts only.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Print first 10 insertions for each table.",
    )
    args = parser.parse_args()

    from app.models.db_manager import get_db_manager

    if args.commit:
        ctx = get_db_manager().transaction(op="backfill_kg_evidence")
    else:
        ctx = get_db_manager().read_session()

    print(f"=== KG evidence backfill — {'COMMIT' if args.commit else 'DRY-RUN'} ===")
    print(f"Cutoff: claim_proposal.created_at >= '{REBUILD_CUTOFF_ISO}'\n")

    with ctx as session:
        print("--- Node evidence backfill ---")
        node_stats = _backfill_node_evidence(
            session, commit=args.commit, verbose=args.verbose,
        )
        print(f"  scanned                  : {node_stats['scanned']}")
        print(f"  skipped_no_resolution    : {node_stats['skipped_no_resolution']}")
        print(f"  skipped_already_present  : {node_stats['skipped_already_present']}")
        print(f"  skipped_no_evidence      : {node_stats['skipped_no_evidence']}")
        print(f"  would_insert / inserted  : {node_stats['would_insert']} / {node_stats['inserted']}")
        print(f"  by merge_action          : {node_stats['by_action']}")

        print("\n--- Edge evidence backfill ---")
        edge_stats = _backfill_edge_evidence(
            session, commit=args.commit, verbose=args.verbose,
        )
        print(f"  scanned                  : {edge_stats['scanned']}")
        print(f"  skipped_already_present  : {edge_stats['skipped_already_present']}")
        print(f"  skipped_no_evidence      : {edge_stats['skipped_no_evidence']}")
        print(f"  would_insert / inserted  : {edge_stats['would_insert']} / {edge_stats['inserted']}")
        print(f"  by merge_action          : {edge_stats['by_action']}")

    print(f"\n=== {'COMMITTED' if args.commit else 'DRY-RUN COMPLETE'} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
