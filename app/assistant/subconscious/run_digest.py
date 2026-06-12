"""CLI to produce + post the subconscious daily digest.

Thin wrapper over ``digest_runner.run_digest_pass`` — the SAME pass the
``subconscious_digest`` routine runs daily. Use the CLI for manual runs
and previews.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_digest
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_digest --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_digest --no-post

Flags:
    --dry-run   Render only. Don't write file, don't post, don't touch state.
    --no-post   Write file but don't post to chat.
    --room-id   Override target room (default: master_room).
    --force     Re-include concerns even if previously surfaced.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.subconscious.digest_builder import load_digest_state, render_digest
from app.assistant.subconscious.digest_runner import (
    load_latest_pending_questions,
    load_register,
    run_digest_pass,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Render + post the subconscious daily digest.")
    parser.add_argument("--dry-run", action="store_true", help="Render to stdout only.")
    parser.add_argument("--no-post", action="store_true", help="Write file but don't post to chat.")
    parser.add_argument("--room-id", default="master_room", help="Target chat room (default: master_room).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Treat every active concern as NEW (ignore previously-surfaced set).",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("SUBCONSCIOUS DIGEST")
    print(f"started_at_utc: {datetime.now(timezone.utc).isoformat()}")
    print(f"room_id:        {args.room_id}")
    print("=" * 70)

    if args.dry_run:
        register = load_register()
        state = load_digest_state()
        previously = set() if args.force else set(state.get("previously_surfaced_concern_ids") or [])
        digest_text = render_digest(
            register=register,
            previously_surfaced_ids=previously,
            pending_questions=load_latest_pending_questions(),
        )
        print("\n--dry-run — rendered digest (no writes):\n")
        print("─" * 70)
        print(digest_text)
        print("─" * 70)
        return

    summary = run_digest_pass(
        room_id=args.room_id,
        force=args.force,
        post=not args.no_post,
    )
    print(json.dumps(summary, indent=2))
    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
