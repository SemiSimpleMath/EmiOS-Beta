"""CLI to run the meal-feedback pass once (produce questions + ingest answers).

Thin wrapper over meal_feedback_runner.run_meal_feedback_pass — the SAME pass the
hourly meal_feedback_run routine uses. PRODUCE: enqueue "How was <dish>?" for
recent past meals. INGEST: turn the user's chat reply into a feedback.comment pod
(-> feedback_extractor -> beliefs).

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_meal_feedback
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_meal_feedback --dry-run
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import app.assistant.tests.test_setup  # noqa: F401  (bootstraps DI for standalone use)

from app.assistant.subconscious.meal_feedback_runner import run_meal_feedback_pass  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Run the meal-feedback pass once.")
    parser.add_argument("--dry-run", action="store_true", help="Count what would happen; don't enqueue/mint.")
    args = parser.parse_args()

    print("=" * 70)
    print(f"MEAL FEEDBACK PASS — dry_run={args.dry_run}")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)

    summary = run_meal_feedback_pass(dry_run=args.dry_run)
    for k, v in summary.items():
        print(f"{k}: {v}")
    print("\nDONE")


if __name__ == "__main__":
    main()
