"""One-off cleanup of the Collaboration State node 30a42722:

  (a) Delete the misattached edge: 'Theme Exploration Preference' → about →
      Collaboration. The sentence ("Jukka wants to explore collaboration
      as a naming theme for the agent system") is about using the WORD
      'collaboration' as a naming theme, not about the actual Jukka-Emi
      collaboration. Got attached via label-match on the word.

  (b) Dedupe the two pairs of collaborator edges:
      - is_collaborator_in Jukka  (dated 2026-02-18)  ← keep
      - collaborator_in    Jukka  (no evidence)        ← delete
      - is_collaborator_in Emi    (dated 2026-02-18)  ← keep
      - collaborator_in    Emi    (no evidence)        ← delete

Usage:
    PYTHONPATH=. .venv/Scripts/python.exe scripts/cleanup_collaboration_node.py
    PYTHONPATH=. .venv/Scripts/python.exe scripts/cleanup_collaboration_node.py --commit
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone

import app.assistant.tests.test_setup  # noqa: F401  — bootstraps DI

from sqlalchemy import text as sql_text

from app.models.db_manager import get_db_manager


COLLAB_PREFIX = "30a42722"


def main():
    commit = "--commit" in sys.argv
    print(f"=== Collaboration cleanup — {'COMMIT' if commit else 'DRY-RUN'} ===\n")

    with get_db_manager().transaction(op="cleanup_collaboration_node") as s:
        collab_id = s.execute(sql_text(
            "SELECT id FROM kg_node_metadata WHERE id LIKE :p AND label = 'Collaboration'"
        ), {"p": COLLAB_PREFIX + "%"}).scalar()
        if collab_id is None:
            print(f"!! Collaboration node {COLLAB_PREFIX} not found — aborting.")
            return

        edges = s.execute(sql_text("""
            SELECT e.id, e.source_id, e.target_id, e.relationship_type, e.sentence,
                   sn.label AS src_lbl, tn.label AS tgt_lbl,
                   (SELECT MAX(ee.message_timestamp) FROM kg_edge_evidence ee
                    WHERE ee.edge_id = e.id) AS evidence_ts
            FROM kg_edge_metadata e
            JOIN kg_node_metadata sn ON sn.id = e.source_id
            JOIN kg_node_metadata tn ON tn.id = e.target_id
            WHERE e.source_id = :p OR e.target_id = :p
        """), {"p": collab_id}).fetchall()

        # (a) Find the misattached "Theme Exploration Preference" edge.
        # The predicate happens to be `topic` (not `about` — fixed during
        # dry-run discovery).
        misattached: list = []
        for e in edges:
            other_lbl = e.src_lbl if e.target_id == collab_id else e.tgt_lbl
            if other_lbl == "Theme Exploration Preference" and e.relationship_type == "topic":
                misattached.append(e)

        # (b) Find duplicate-predicate collaborator pairs to dedupe.
        #     Group <-collaborator_in / <-is_collaborator_in by other-endpoint.
        collab_pairs: dict[str, dict[str, list]] = {}  # other_label → {predicate → [edges]}
        for e in edges:
            if e.target_id != collab_id:
                continue
            if e.relationship_type not in ("collaborator_in", "is_collaborator_in"):
                continue
            collab_pairs.setdefault(e.src_lbl, {}).setdefault(e.relationship_type, []).append(e)

        to_delete_dupe: list = []
        for who, by_pred in collab_pairs.items():
            has_keep = "is_collaborator_in" in by_pred
            has_dupe = "collaborator_in" in by_pred
            if has_keep and has_dupe:
                # Delete the dateless 'collaborator_in' duplicates.
                for d in by_pred["collaborator_in"]:
                    if not d.evidence_ts:
                        to_delete_dupe.append(d)

        print("(a) Misattached edge to delete:")
        for e in misattached:
            print(f"  edge {e.id[:8]}  src={e.src_lbl!r}  rel={e.relationship_type}  ts={e.evidence_ts or 'no_ev'}")
            print(f"    {e.sentence!r}")
        print()

        print("(b) Duplicate collaborator edges to delete (keeping dated 'is_collaborator_in'):")
        for e in to_delete_dupe:
            print(f"  edge {e.id[:8]}  src={e.src_lbl!r}  rel={e.relationship_type}  ts={e.evidence_ts or 'no_ev'}")
            print(f"    {e.sentence!r}")
        print()

        edge_ids_to_delete = [e.id for e in misattached + to_delete_dupe]
        print(f"Total edges to delete: {len(edge_ids_to_delete)}")
        print()

        if not commit:
            print("Dry-run — no changes made. Re-run with --commit to execute.")
            return

        # Audit log entry first (records what's about to be deleted).
        before = []
        for e in misattached + to_delete_dupe:
            before.append({
                "edge_id": e.id, "predicate": e.relationship_type,
                "src": e.src_lbl, "tgt": e.tgt_lbl, "sentence": e.sentence,
            })
        s.execute(sql_text(
            "INSERT INTO kg_revision_log (id, op, args_json, before_json, after_json, reason, succeeded, created_at) "
            "VALUES (:id, :op, :args, :before, :after, :reason, 1, :ts)"
        ), {
            "id": str(uuid.uuid4()),
            "op": "cleanup_collaboration_edges",
            "args": json.dumps({"node_id": collab_id, "deleted_edge_ids": edge_ids_to_delete}),
            "before": json.dumps(before),
            "after": json.dumps({"deleted_count": len(edge_ids_to_delete)}),
            "reason": "Cleanup: misattached Theme Exploration edge + duplicate collaborator_in edges",
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        # Delete edge_evidence rows first (FK), then edges.
        for eid in edge_ids_to_delete:
            s.execute(sql_text("DELETE FROM kg_edge_evidence WHERE edge_id = :e"), {"e": eid})
            s.execute(sql_text("DELETE FROM kg_edge_metadata WHERE id = :e"), {"e": eid})
        print(f"  ✓ Deleted {len(edge_ids_to_delete)} edges (+ their evidence rows)")
        print(f"  ✓ Logged kg_revision_log op=cleanup_collaboration_edges")

        # Verify
        remaining = s.execute(sql_text(
            "SELECT COUNT(*) FROM kg_edge_metadata WHERE source_id = :p OR target_id = :p"
        ), {"p": collab_id}).scalar()
        print(f"  ✓ Verified: {remaining} edges remain on Collaboration node (was 13)")
        print()
        print("Cleanup complete. Transaction committing.")


if __name__ == "__main__":
    main()
