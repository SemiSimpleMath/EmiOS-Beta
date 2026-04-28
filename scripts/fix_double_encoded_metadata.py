"""
One-time fix: repair double-encoded metadata_json in unified_log_2026.

Some rows have metadata_json stored as a JSON string of a JSON string
(e.g. '"{\\\"item_id\\\": ...}"' instead of '{"item_id": ...}').
This causes _row_to_dict to discard the metadata, making items invisible.

Dry-run by default. Pass --apply to write.

Usage:
    .venv/Scripts/python.exe scripts/fix_double_encoded_metadata.py          # dry run
    .venv/Scripts/python.exe scripts/fix_double_encoded_metadata.py --apply  # write
"""
import sys
import json
import sqlite3
import os

# Handle Windows console encoding
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

DRY_RUN = "--apply" not in sys.argv
DB_PATH = "emi.db"


def main():
    if DRY_RUN:
        print("=== DRY RUN (pass --apply to write changes) ===\n")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""
        SELECT id, metadata_json FROM unified_log_2026
        WHERE metadata_json LIKE '"%'
    """)
    rows = cur.fetchall()

    fixed = 0
    errors = 0
    for row_id, raw in rows:
        try:
            # First parse: gets the inner JSON string
            inner = json.loads(raw)
            if not isinstance(inner, str):
                continue  # Not double-encoded
            # Second parse: gets the actual dict
            metadata = json.loads(inner)
            if not isinstance(metadata, dict):
                continue

            fixed += 1
            sid = metadata.get("short_id", "")
            state = metadata.get("state", "")
            src = metadata.get("source_type", "")
            summary = str(metadata.get("summary", ""))[:60]
            print(f"  FIX {row_id[:35]:35} | sid:{str(sid):5} | {state:12} | {src:15} | {summary}")

            if not DRY_RUN:
                # Write back the properly-encoded JSON
                conn.execute(
                    "UPDATE unified_log_2026 SET metadata_json = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), row_id),
                )
        except (json.JSONDecodeError, TypeError) as e:
            errors += 1
            print(f"  ERR {row_id[:35]}: {e}")

    if not DRY_RUN and fixed > 0:
        conn.commit()

    conn.close()

    print(f"\n{'Would fix' if DRY_RUN else 'Fixed'} {fixed} row(s). Errors: {errors}.")
    if DRY_RUN and fixed > 0:
        print("Pass --apply to write changes.")


if __name__ == "__main__":
    main()
