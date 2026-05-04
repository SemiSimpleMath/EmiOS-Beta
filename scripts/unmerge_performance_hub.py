"""One-off forensic split of the over-merged Performance hub 0b4416dd.

Splits the hub into two new Event nodes (Beetlejuice + Drowsy Chaperone) per
the categorization confirmed by Jukka 2026-05-03. Run dry-run first; rerun
with --commit to actually execute.

References to handle:
  - 14 edges (kg_edge_metadata.source_id / target_id) — repoint each to
    the appropriate new node based on the agreed split
  - 6 edge_evidence rows — stay attached to their edges, no action needed
  - 1 node_evidence row — move to Beetlejuice (most original content)
  - 1 claim_proposal_node.resolved_node_id pointer — move to Drowsy
    Chaperone (that proposal was the 2026-05-03 Drowsy Chaperone one)
  - 2 node_taxonomy_links rows — duplicate to BOTH new nodes (the
    taxonomy classification ('Event subtype') applies to both)
  - 1 kg_node_metadata row (the Performance hub itself) — DELETE last
  - kg_revision_log — append op='split_node' with before/after JSON

Usage::
    PYTHONPATH=. .venv\\Scripts\\python.exe scripts/unmerge_performance_hub.py
    PYTHONPATH=. .venv\\Scripts\\python.exe scripts/unmerge_performance_hub.py --commit
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone

import app.assistant.tests.test_setup  # noqa: F401  — bootstraps DI

from sqlalchemy import text as sql_text

from app.models.db_manager import get_db_manager


PERF_ID = "0b4416dd-8b39-49c2-8573-dd83f79f6b0b"

# Categorization per Jukka 2026-05-03. Edge id-prefixes mapped to "B" or "D".
# (Filled in below from a SELECT — we don't hardcode UUIDs here so the script
# stays robust if the script is rerun after partial state.)


def _make_new_node(label: str, original_sentence: str, start_date_iso: str | None) -> dict:
    """Build a minimal kg_node_metadata INSERT row for the split-out Event."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": str(uuid.uuid4()),
        "label": label,
        "node_type": "Event",
        "category": "stage performance",
        "description": "",
        "aliases": json.dumps([]),
        "attributes": json.dumps({"first_observed": start_date_iso or now}),
        "original_sentence": original_sentence,
        "start_date": start_date_iso,
        "end_date": start_date_iso,  # one-day events
        "valid_during": "one-off",
        "created_at": now,
        "updated_at": now,
        "confidence": 0.95,
        "importance": 5.0,
        "source": "manual_split:0b4416dd",
    }


def main():
    commit = "--commit" in sys.argv
    mode = "COMMIT" if commit else "DRY-RUN"
    print(f"=== Performance hub split — {mode} ===\n")

    with get_db_manager().transaction(op="unmerge_performance_hub") as s:
        # 1. Verify the hub still exists.
        hub = s.execute(sql_text(
            "SELECT id, label, node_type, original_sentence FROM kg_node_metadata WHERE id = :p"
        ), {"p": PERF_ID}).fetchone()
        if hub is None:
            print(f"!! Performance hub {PERF_ID[:8]} not found — already split or deleted? Aborting.")
            return
        print(f"Hub:  {hub.label!r} [{hub.node_type}]  {hub.id}")

        # 2. Pull all 14 edges with the data we need to categorize.
        edges = s.execute(sql_text("""
            SELECT e.id, e.source_id, e.target_id, e.relationship_type, e.sentence,
                   sn.label AS src_lbl, tn.label AS tgt_lbl,
                   (SELECT MAX(ee.message_timestamp) FROM kg_edge_evidence ee WHERE ee.edge_id = e.id) AS evidence_ts
            FROM kg_edge_metadata e
            JOIN kg_node_metadata sn ON sn.id = e.source_id
            JOIN kg_node_metadata tn ON tn.id = e.target_id
            WHERE e.source_id = :p OR e.target_id = :p
            ORDER BY evidence_ts NULLS LAST, e.created_at
        """), {"p": PERF_ID}).fetchall()
        print(f"Edges: {len(edges)} touching the hub\n")

        # 3. Categorize each edge into 'B' (Beetlejuice) or 'D' (Drowsy
        #    Chaperone) per the rules confirmed with Jukka. The rules are
        #    keyword-based so they're auditable; ambiguous cases default to B.
        def categorize(edge) -> str:
            sent = (edge.sentence or "").lower()
            other_lbl = (edge.tgt_lbl if edge.source_id == PERF_ID else edge.src_lbl).lower()
            # Drowsy Chaperone signals
            if "drowsy chaperone" in sent or "drowsy chaperone" in other_lbl:
                return "D"
            if "rehearsal" in other_lbl or "call time" in other_lbl or "enjoyment" in other_lbl:
                return "D"
            ts = (edge.evidence_ts or "")
            if ts.startswith("2026-03-21"):
                return "D"  # the Drowsy day-of chat
            # Beetlejuice signals
            if "south lake middle school" in sent or "south lake middle school" in other_lbl:
                return "B"
            if other_lbl in ("annika", "jorma", "seija", "visit", "conversation", "musical theater"):
                return "B"
            if ts.startswith("2025-10-26"):
                return "B"  # the visit-anticipation chat
            return "B"  # default — fewer surprises (older context)

        beetle_edges = []
        drowsy_edges = []
        for e in edges:
            cat = categorize(e)
            (beetle_edges if cat == "B" else drowsy_edges).append(e)

        def _line(e):
            is_src = (e.source_id == PERF_ID)
            other = e.tgt_lbl if is_src else e.src_lbl
            arrow = "->" if is_src else "<-"
            return f"  {arrow} {e.relationship_type:<22} {other!r:<55}  ts={(e.evidence_ts or '(none)')[:19]}"

        print(f"-- Beetlejuice ({len(beetle_edges)} edges) --")
        for e in beetle_edges:
            print(_line(e))
        print(f"\n-- Drowsy Chaperone ({len(drowsy_edges)} edges) --")
        for e in drowsy_edges:
            print(_line(e))
        print()

        # 4. Build the two new node rows.
        beetle = _make_new_node(
            label="Beetlejuice Performance",
            original_sentence="Annika performed in the Beetlejuice musical at South Lake Middle School.",
            start_date_iso="2025-10-26",  # earliest evidence timestamp
        )
        drowsy = _make_new_node(
            label="Drowsy Chaperone Performance",
            original_sentence="Jukka's children performed in The Drowsy Chaperone on March 21, 2026.",
            start_date_iso="2026-03-21",  # confirmed by Jukka
        )
        print(f"NEW: Beetlejuice Performance       {beetle['id']}")
        print(f"NEW: Drowsy Chaperone Performance  {drowsy['id']}")
        print()

        # 5. Plan the SQL we'd execute (dry-run): show counts.
        b_id = beetle["id"]
        d_id = drowsy["id"]

        node_evidence_cnt = s.execute(sql_text(
            "SELECT COUNT(*) FROM kg_node_evidence WHERE node_id = :p"
        ), {"p": PERF_ID}).scalar()
        prop_node_cnt = s.execute(sql_text(
            "SELECT COUNT(*) FROM claim_proposal_node WHERE resolved_node_id = :p"
        ), {"p": PERF_ID}).scalar()
        tax_links_cnt = s.execute(sql_text(
            "SELECT COUNT(*) FROM node_taxonomy_links WHERE node_id = :p"
        ), {"p": PERF_ID}).scalar()

        print("Operations plan:")
        print(f"  INSERT 2 nodes (Beetlejuice + Drowsy Chaperone)")
        print(f"  UPDATE {len(beetle_edges)} edges → Beetlejuice ({b_id[:8]})")
        print(f"  UPDATE {len(drowsy_edges)} edges → Drowsy Chaperone ({d_id[:8]})")
        print(f"  UPDATE {node_evidence_cnt} kg_node_evidence rows → Beetlejuice")
        print(f"  UPDATE {prop_node_cnt} claim_proposal_node.resolved_node_id rows → Drowsy Chaperone")
        print(f"  DUPLICATE {tax_links_cnt} node_taxonomy_links rows to BOTH new nodes, then DELETE originals")
        print(f"  INSERT 1 kg_revision_log row (op=split_node)")
        print(f"  DELETE 1 kg_node_metadata row (the original Performance hub)")
        print()

        if not commit:
            print("Dry-run — no changes made. Re-run with --commit to execute.")
            return

        # 6. EXECUTE.
        print("Executing...")

        node_cols = list(beetle.keys())
        col_names = ", ".join(node_cols)
        col_binds = ", ".join(f":{c}" for c in node_cols)
        s.execute(sql_text(f"INSERT INTO kg_node_metadata ({col_names}) VALUES ({col_binds})"), beetle)
        s.execute(sql_text(f"INSERT INTO kg_node_metadata ({col_names}) VALUES ({col_binds})"), drowsy)
        print(f"  ✓ Inserted {beetle['label']!r} and {drowsy['label']!r}")

        for e in beetle_edges:
            if e.source_id == PERF_ID:
                s.execute(sql_text("UPDATE kg_edge_metadata SET source_id = :n WHERE id = :e"),
                          {"n": b_id, "e": e.id})
            else:
                s.execute(sql_text("UPDATE kg_edge_metadata SET target_id = :n WHERE id = :e"),
                          {"n": b_id, "e": e.id})
        print(f"  ✓ Repointed {len(beetle_edges)} Beetlejuice edges")

        for e in drowsy_edges:
            if e.source_id == PERF_ID:
                s.execute(sql_text("UPDATE kg_edge_metadata SET source_id = :n WHERE id = :e"),
                          {"n": d_id, "e": e.id})
            else:
                s.execute(sql_text("UPDATE kg_edge_metadata SET target_id = :n WHERE id = :e"),
                          {"n": d_id, "e": e.id})
        print(f"  ✓ Repointed {len(drowsy_edges)} Drowsy Chaperone edges")

        s.execute(sql_text("UPDATE kg_node_evidence SET node_id = :n WHERE node_id = :p"),
                  {"n": b_id, "p": PERF_ID})
        print(f"  ✓ Moved {node_evidence_cnt} node_evidence rows to Beetlejuice")

        s.execute(sql_text("UPDATE claim_proposal_node SET resolved_node_id = :n WHERE resolved_node_id = :p"),
                  {"n": d_id, "p": PERF_ID})
        print(f"  ✓ Moved {prop_node_cnt} claim_proposal_node pointer to Drowsy Chaperone")

        # Duplicate taxonomy links to both new nodes, then delete originals.
        tax_rows = s.execute(sql_text(
            "SELECT * FROM node_taxonomy_links WHERE node_id = :p"
        ), {"p": PERF_ID}).fetchall()
        for row in tax_rows:
            cols_meta = s.execute(sql_text("PRAGMA table_info(node_taxonomy_links)")).fetchall()
            col_list = [c[1] for c in cols_meta]
            for new_id in (b_id, d_id):
                vals = {c: getattr(row, c) for c in col_list}
                vals["node_id"] = new_id
                if "id" in vals:
                    vals["id"] = str(uuid.uuid4())  # avoid PK collision
                placeholders = ", ".join(f":{c}" for c in col_list)
                s.execute(sql_text(
                    f"INSERT INTO node_taxonomy_links ({', '.join(col_list)}) VALUES ({placeholders})"
                ), vals)
        s.execute(sql_text("DELETE FROM node_taxonomy_links WHERE node_id = :p"), {"p": PERF_ID})
        print(f"  ✓ Duplicated {tax_links_cnt} taxonomy_links to both new nodes")

        # Audit log entry.
        before = {
            "id": PERF_ID,
            "label": hub.label,
            "node_type": hub.node_type,
            "original_sentence": hub.original_sentence,
            "edge_count": len(edges),
        }
        after = {
            "beetlejuice_id": b_id,
            "drowsy_chaperone_id": d_id,
            "beetlejuice_edge_count": len(beetle_edges),
            "drowsy_edge_count": len(drowsy_edges),
        }
        rev_id = str(uuid.uuid4())
        s.execute(sql_text(
            "INSERT INTO kg_revision_log (id, op, args_json, before_json, after_json, reason, succeeded, created_at) "
            "VALUES (:id, :op, :args, :before, :after, :reason, 1, :ts)"
        ), {
            "id": rev_id, "op": "split_node",
            "args": json.dumps({"fold_id": PERF_ID, "into": [b_id, d_id]}),
            "before": json.dumps(before),
            "after": json.dumps(after),
            "reason": "Forensic unmerge of over-merged Performance hub (Beetlejuice + Drowsy Chaperone)",
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        print(f"  ✓ Logged kg_revision_log id={rev_id[:8]}")

        # Finally, delete the now-empty Performance node.
        s.execute(sql_text("DELETE FROM kg_node_metadata WHERE id = :p"), {"p": PERF_ID})
        print(f"  ✓ Deleted original Performance hub")

        # Verify within the same transaction.
        remaining = s.execute(sql_text(
            "SELECT COUNT(*) FROM kg_edge_metadata WHERE source_id = :p OR target_id = :p"
        ), {"p": PERF_ID}).scalar()
        if remaining > 0:
            raise RuntimeError(f"After split, {remaining} edges still touch the deleted hub — rolling back")
        print(f"  ✓ Verified: 0 edges remain on the deleted hub")
        print()
        print("Split complete. Transaction committing.")


if __name__ == "__main__":
    main()
