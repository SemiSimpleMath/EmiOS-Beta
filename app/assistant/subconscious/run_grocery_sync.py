"""CLI to sync grocery inventory from recent chat + apply decay.

Thin wrapper over ``grocery_sync_runner.run_grocery_sync_pass`` — the SAME pass the daily
``grocery_sync_run`` routine uses. Steps: scan recent user chat (grocery_intent_scanner)
-> apply intents (grocery_inventory.apply_*) -> apply daily decay.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_grocery_sync
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_grocery_sync --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_grocery_sync --hours 24 --scan-all
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import app.assistant.tests.test_setup  # noqa: F401  (bootstraps DI for standalone use)

from app.assistant.subconscious.grocery_inventory import render_inventory_summary  # noqa: E402
from app.assistant.subconscious.grocery_sync_runner import run_grocery_sync_pass  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Sync grocery inventory from recent chat.")
    parser.add_argument("--hours", type=int, default=48, help="Chat lookback window (default 48h).")
    parser.add_argument("--dry-run", action="store_true", help="Detect intents but don't apply.")
    parser.add_argument("--scan-all", action="store_true",
                        help="Ignore previously-scanned set; re-scan the whole window.")
    args = parser.parse_args()

    print("=" * 70)
    print(f"GROCERY SYNC — lookback={args.hours}h dry_run={args.dry_run}")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    summary = run_grocery_sync_pass(hours=args.hours, dry_run=args.dry_run, scan_all=args.scan_all)
    for k, v in summary.items():
        print(f"{k}: {v}")

    print("\n" + "-" * 70)
    print("Current inventory snapshot:")
    print(render_inventory_summary())
    print("-" * 70 + "\nDONE")


if __name__ == "__main__":
    main()
