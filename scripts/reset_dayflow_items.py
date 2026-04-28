"""
Reset dayflow items: close all items from today and yesterday.

This gives a clean slate after structural changes to the dayflow
orchestrator. Items are set to state='closed' with reason, not deleted.

Dry-run by default. Pass --apply to actually write.

Usage:
    .venv/Scripts/python.exe scripts/reset_dayflow_items.py          # dry run
    .venv/Scripts/python.exe scripts/reset_dayflow_items.py --apply  # write changes
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.assistant.tests.test_setup  # noqa: F401

import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, update

from app.models.base import get_session
from app.assistant.database.db_handler import UnifiedLog2026

DRY_RUN = "--apply" not in sys.argv
# Close items from the last 2 days.
CUTOFF = datetime.now(timezone.utc) - timedelta(days=2)


def main():
    if DRY_RUN:
        print("=== DRY RUN (pass --apply to write changes) ===\n")

    with get_session() as session:
        rows = session.execute(
            select(UnifiedLog2026)
            .where(UnifiedLog2026.source == "dayflow_item")
            .where(UnifiedLog2026.room_id == "dayflow_orchestrator")
        ).scalars().all()

        to_close = []

        for row in rows:
            meta = {}
            if row.metadata_json:
                try:
                    meta = json.loads(row.metadata_json) if isinstance(row.metadata_json, str) else row.metadata_json
                except (json.JSONDecodeError, TypeError):
                    continue

            if not isinstance(meta, dict):
                continue

            state = str(meta.get("state") or "").strip().lower()
            if state == "closed":
                continue

            source_type = str(meta.get("source_type") or "").strip().lower()
            item_id = str(meta.get("item_id") or row.id or "").strip()
            summary = str(meta.get("summary") or "").strip()[:60]
            created_at = str(meta.get("created_at") or "").strip()

            # Check timestamp — close items from last 2 days.
            try:
                if created_at:
                    ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < CUTOFF:
                        continue
                elif row.timestamp:
                    if row.timestamp < CUTOFF:
                        continue
            except (ValueError, TypeError):
                pass

            to_close.append((row, item_id, state, source_type, summary))

        # Also close dayflow_request items (user delegations awaiting triage).
        request_rows = session.execute(
            select(UnifiedLog2026)
            .where(UnifiedLog2026.source == "dayflow_request")
            .where(UnifiedLog2026.room_id == "dayflow_orchestrator")
        ).scalars().all()

        for row in request_rows:
            meta = {}
            if row.metadata_json:
                try:
                    meta = json.loads(row.metadata_json) if isinstance(row.metadata_json, str) else row.metadata_json
                except (json.JSONDecodeError, TypeError):
                    continue
            if not isinstance(meta, dict):
                continue
            if meta.get("ingested_at"):
                continue
            item_id = str(meta.get("request_id") or row.id or "").strip()
            summary = str(meta.get("summary") or "").strip()[:60]
            to_close.append((row, item_id, "pending", "dayflow_request", summary))

        print(f"Found {len(to_close)} item(s) to close:\n")
        for row, item_id, state, source_type, summary in to_close:
            print(f"  [{source_type}] {state:15s} | {item_id[:40]:40s} | {summary}")

        if DRY_RUN:
            print(f"\n=== DRY RUN — {len(to_close)} item(s) would be closed. Pass --apply to write. ===")
            return

        closed_count = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        for row, item_id, state, source_type, summary in to_close:
            meta = {}
            if row.metadata_json:
                try:
                    meta = json.loads(row.metadata_json) if isinstance(row.metadata_json, str) else row.metadata_json
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}

            meta["state"] = "closed"
            meta["state_reason"] = "manual_reset"
            meta["closed_at"] = now_iso

            # For dayflow_request items, also mark as ingested so they don't re-enter.
            if source_type == "dayflow_request":
                meta["ingested_at"] = now_iso

            session.execute(
                update(UnifiedLog2026)
                .where(UnifiedLog2026.id == row.id)
                .values(metadata_json=json.dumps(meta, ensure_ascii=False))
            )
            closed_count += 1

        session.commit()
        print(f"\n=== Closed {closed_count} item(s). Clean slate. ===")


if __name__ == "__main__":
    main()
