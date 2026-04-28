"""
One-time cleanup: close stale dayflow items (old emails, calendar events,
and duplicate action log entries).

Safe: sets state to 'closed' with reason — does not delete any rows.
Dry-run by default. Pass --apply to actually write.

Usage:
    .venv/Scripts/python.exe scripts/cleanup_stale_dayflow_items.py          # dry run
    .venv/Scripts/python.exe scripts/cleanup_stale_dayflow_items.py --apply  # write changes
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.assistant.tests.test_setup  # noqa: F401 — bootstrap DI

import json
from datetime import datetime, timezone
from sqlalchemy import select, update

from app.models.base import get_session
from app.assistant.database.db_handler import UnifiedLog2026

DRY_RUN = "--apply" not in sys.argv
CUTOFF_DATE = "2026-04-02"  # close anything created before this date

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
        action_log_dupes = {}  # key → list of rows (keep newest, close rest)

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
            source_type = str(meta.get("source_type") or "").strip().lower()
            item_id = str(meta.get("item_id") or row.id or "").strip()
            created_at = str(meta.get("created_at") or "").strip()

            # 1. Close all calendar items (no longer ingested)
            if source_type == "calendar" and state not in ("closed",):
                to_close.append((row, item_id, f"calendar item: {meta.get('summary', '')[:500]}", "cleanup_calendar_removed"))
                continue

            # 2. Close old emails (before cutoff date) with state 'new'
            if source_type == "email" and state == "new" and created_at < CUTOFF_DATE:
                to_close.append((row, item_id, f"old email: {meta.get('summary', '')[:500]}", "cleanup_stale_email"))
                continue

            # 3. Track duplicate action_log/action_result entries for dedup
            if source_type in ("action_result", "action_selector_action", "action_log"):
                summary = str(meta.get("summary") or "").strip()
                task_id = str(meta.get("task_id") or meta.get("acted_on_item_id") or "").strip()
                dedup_key = f"{source_type}|{task_id}|{summary}"
                action_log_dupes.setdefault(dedup_key, []).append((row, item_id, meta))

        # 4. For duplicate action log entries, keep the newest, close the rest
        for key, entries in action_log_dupes.items():
            if len(entries) <= 1:
                continue
            # Sort by created_at, keep the newest
            entries.sort(key=lambda e: str(e[2].get("created_at") or ""), reverse=True)
            for row, item_id, meta in entries[1:]:  # skip first (newest)
                state = str(meta.get("state") or "").strip().lower()
                if state == "closed":
                    continue
                to_close.append((row, item_id, f"duplicate action_log: {meta.get('summary', '')[:500]}", "cleanup_duplicate_action_log"))

        print(f"Found {len(to_close)} item(s) to close:\n")
        for row, item_id, desc, reason in to_close:
            print(f"  [{reason}] {item_id}: {desc}")

        if DRY_RUN:
            print(f"\n=== DRY RUN — {len(to_close)} item(s) would be closed. Pass --apply to write. ===")
            return

        # Apply changes
        closed_count = 0
        for row, item_id, desc, reason in to_close:
            meta = {}
            if row.metadata_json:
                try:
                    meta = json.loads(row.metadata_json) if isinstance(row.metadata_json, str) else row.metadata_json
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}

            meta["state"] = "closed"
            meta["state_reason"] = reason
            meta["closed_at"] = datetime.now(timezone.utc).isoformat()

            session.execute(
                update(UnifiedLog2026)
                .where(UnifiedLog2026.id == row.id)
                .values(metadata_json=json.dumps(meta, ensure_ascii=False))
            )
            closed_count += 1

        session.commit()
        print(f"\n=== Closed {closed_count} item(s). ===")


if __name__ == "__main__":
    main()
