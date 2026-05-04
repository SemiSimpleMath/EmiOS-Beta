"""Bulk delete the grocery/shopping cluster — Jukka's call (2026-05-03):
'delete all the grocery stuff who cares.'

Scope (drop all of these + their edges):

  States — labels matching grocery / shopping / aisle / deli / sausage /
    salad / produce / meal side / etc.
  Entities — aisle/section Entities that exist primarily to support the
    grocery State graph (Snack Aisle, Dairy Aisle, Bread Aisle, Meat/Fish
    Section, Produce Section, Fridge Section, Deli/Sushi Section).

Out of scope (debatable but kept):
  - "Food Preference" — could be useful life-fact (e.g., dietary)
  - "Food ordering habit" — could indicate lifestyle (eats out vs cooks)
  Re-add to LABEL_PATTERNS below if Jukka wants those gone too.

Default dry-run; --commit to execute. Single transaction with audit log.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone

import app.assistant.tests.test_setup  # noqa: F401  — bootstraps DI

from sqlalchemy import text as sql_text

from app.models.db_manager import get_db_manager


# State labels to delete (case-insensitive substring match against label).
STATE_LABEL_PATTERNS = [
    "grocery",
    "shopping",
    "sausage",
    "deli treats",
    "salad and produce",
    "meal side",
]

# Entity labels to delete — aisle/section names that exist mostly to
# support the grocery graph. Extra strict because these names could
# conceivably reappear in other contexts; we just delete the named ones
# we already enumerated.
ENTITY_LABELS_TO_DELETE = [
    "Snack Aisle",
    "Dairy Aisle",
    "Bread Aisle",
    "Meat/Fish Section",
    "Produce Section",
    "Fridge section",
    "Deli/Sushi Section",
]


def main():
    commit = "--commit" in sys.argv
    print(f"=== Grocery cluster deletion — {'COMMIT' if commit else 'DRY-RUN'} ===\n")

    with get_db_manager().transaction(op="delete_grocery_cluster") as s:
        # 1. Find all in-scope nodes.
        state_clauses = " OR ".join(
            [f"lower(label) LIKE '%{p}%'" for p in STATE_LABEL_PATTERNS]
        )
        state_rows = s.execute(sql_text(
            f"SELECT id, label FROM kg_node_metadata "
            f"WHERE node_type = 'State' AND ({state_clauses}) "
            "ORDER BY label"
        )).fetchall()

        entity_rows = s.execute(sql_text(
            "SELECT id, label FROM kg_node_metadata "
            "WHERE node_type = 'Entity' AND label IN :labels"
        ).bindparams(__import__("sqlalchemy").bindparam("labels", expanding=True)),
            {"labels": ENTITY_LABELS_TO_DELETE},
        ).fetchall()

        all_ids = [r.id for r in state_rows] + [r.id for r in entity_rows]
        print(f"States in scope ({len(state_rows)}):")
        for r in state_rows:
            cnt = s.execute(sql_text(
                "SELECT COUNT(*) FROM kg_edge_metadata WHERE source_id=:i OR target_id=:i"
            ), {"i": r.id}).scalar()
            print(f"  {r.id[:8]}  edges={cnt:3d}  {r.label!r}")
        print()
        print(f"Entities in scope ({len(entity_rows)}):")
        for r in entity_rows:
            cnt = s.execute(sql_text(
                "SELECT COUNT(*) FROM kg_edge_metadata WHERE source_id=:i OR target_id=:i"
            ), {"i": r.id}).scalar()
            print(f"  {r.id[:8]}  edges={cnt:3d}  {r.label!r}")
        print()

        # 2. Count edges that will cascade-delete.
        edge_cnt = s.execute(sql_text(
            "SELECT COUNT(*) FROM kg_edge_metadata "
            "WHERE source_id IN :ids OR target_id IN :ids"
        ).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": all_ids},
        ).scalar() if all_ids else 0
        print(f"Total edges that will be cascade-deleted: {edge_cnt}")
        print(f"Total nodes to delete: {len(all_ids)}")
        print()

        if not commit:
            print("Dry-run — no changes made. Re-run with --commit to execute.")
            return

        if not all_ids:
            print("Nothing to delete.")
            return

        # 3. Audit log entry.
        before = {
            "states": [{"id": r.id, "label": r.label} for r in state_rows],
            "entities": [{"id": r.id, "label": r.label} for r in entity_rows],
            "edge_count": edge_cnt,
        }
        s.execute(sql_text(
            "INSERT INTO kg_revision_log (id, op, args_json, before_json, after_json, reason, succeeded, created_at) "
            "VALUES (:id, :op, :args, :before, :after, :reason, 1, :ts)"
        ), {
            "id": str(uuid.uuid4()),
            "op": "delete_grocery_cluster",
            "args": json.dumps({"node_ids": all_ids}),
            "before": json.dumps(before),
            "after": json.dumps({"deleted_node_count": len(all_ids), "deleted_edge_count": edge_cnt}),
            "reason": "Jukka 2026-05-03: 'delete all the grocery stuff who cares' — bulk cleanup of mundane day-to-day shopping graph noise",
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        # 4. Delete edges first (be explicit even though FK cascade exists,
        # so a passive_deletes hiccup doesn't leave orphans).
        edge_ids = s.execute(sql_text(
            "SELECT id FROM kg_edge_metadata WHERE source_id IN :ids OR target_id IN :ids"
        ).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": all_ids},
        ).fetchall()
        edge_ids = [r.id for r in edge_ids]
        if edge_ids:
            s.execute(sql_text(
                "DELETE FROM kg_edge_evidence WHERE edge_id IN :ids"
            ).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
                {"ids": edge_ids},
            )
            s.execute(sql_text(
                "DELETE FROM kg_edge_metadata WHERE id IN :ids"
            ).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
                {"ids": edge_ids},
            )
            print(f"  ✓ Deleted {len(edge_ids)} edges (+ their evidence rows)")

        # 5. Delete node-evidence rows for the doomed nodes.
        s.execute(sql_text(
            "DELETE FROM kg_node_evidence WHERE node_id IN :ids"
        ).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": all_ids},
        )
        print(f"  ✓ Deleted node_evidence rows")

        # 6. Delete taxonomy_links + claim_proposal_node pointers.
        s.execute(sql_text(
            "DELETE FROM node_taxonomy_links WHERE node_id IN :ids"
        ).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": all_ids},
        )
        s.execute(sql_text(
            "UPDATE claim_proposal_node SET resolved_node_id = NULL WHERE resolved_node_id IN :ids"
        ).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": all_ids},
        )
        print(f"  ✓ Cleared taxonomy_links + claim_proposal_node pointers")

        # 7. Finally delete the nodes.
        s.execute(sql_text(
            "DELETE FROM kg_node_metadata WHERE id IN :ids"
        ).bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
            {"ids": all_ids},
        )
        print(f"  ✓ Deleted {len(all_ids)} nodes")
        print()
        print("Done. Transaction committing.")


if __name__ == "__main__":
    main()
