"""CLI to produce + post the subconscious daily digest.

Reads the concerns_register (populated by run_noticer ticks), renders a
markdown digest distinguishing new vs ongoing concerns, writes it to
`app/subconscious_digests/digest_YYYY-MM-DD.md`, and posts it to
master_room via OutboundChatPublisher (showing up in chat the next time
the user is connected).

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_digest
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_digest --dry-run
    .venv/Scripts/python.exe -m app.assistant.subconscious.run_digest --no-post

Flags:
    --dry-run   Render only. Don't write file, don't post.
    --no-post   Write file but don't post to chat.
    --room-id   Override target room (default: master_room).
    --force     Re-include concerns even if previously surfaced.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.subconscious.digest_builder import (
    load_digest_state,
    render_digest,
    save_digest_state,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


_REGISTER_REL = "resources/subconscious/resource_concerns_register.json"
_TICK_LOG_REL = "resources/subconscious/resource_subconscious_tick_log.jsonl"
_DIGEST_DIR_REL = "app/subconscious_digests"


def load_register() -> Dict[str, Any]:
    path = get_repo_root() / _REGISTER_REL
    if not path.is_file():
        return {"active": [], "addressing": [], "resolved": [], "dormant": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _persist_digest_to_unified_log(
    *,
    digest_text: str,
    room_id: str,
    digest_date: str,
) -> bool:
    """Append the digest as an assistant message in unified_log_2026.

    This is what makes the digest appear in the user's chat history when
    master_room loads, regardless of whether socketio relays are currently
    running. Source is `subconscious_digest` so KG / extraction pipelines
    can route it differently than normal chat if needed.
    """
    try:
        import uuid as _uuid
        from app.assistant.database.db_handler import UnifiedLog2026
        from app.models.base import get_session

        row_id = f"subconscious_digest:{digest_date}:{_uuid.uuid4().hex[:12]}"
        now_utc = datetime.now(timezone.utc)
        session = get_session()
        try:
            row = UnifiedLog2026(
                id=row_id,
                timestamp=now_utc,
                role="assistant",
                message=digest_text,
                source="subconscious_digest",
                processed=False,
                room_id=room_id,
                room_surface="socketio",
                direction="outbound",
                speaker_name="Subconscious",
                speaker_role="subconscious",
                content_type="markdown",
                metadata_json={
                    "digest_date": digest_date,
                    "produced_by": "subconscious.run_digest",
                    "sub_data_type": "subconscious_digest",
                },
            )
            session.add(row)
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as e:
        logger.warning("[digest] unified_log write failed: %s", e)
        return False


def load_latest_pending_questions() -> List[Dict[str, Any]]:
    """The noticer puts pending_questions in each tick's output, not in the
    register. Read the last tick's output to surface them in the digest."""
    path = get_repo_root() / _TICK_LOG_REL
    if not path.is_file():
        return []
    try:
        last_line = ""
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last_line = line
        if not last_line:
            return []
        d = json.loads(last_line)
        return d.get("output", {}).get("pending_questions") or []
    except Exception as e:
        logger.warning("[digest] failed to load pending_questions from tick log: %s", e)
        return []


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

    # 1. Load state
    register = load_register()
    state = load_digest_state()
    pending_questions = load_latest_pending_questions()

    previously_surfaced: Set[str] = (
        set() if args.force else set(state.get("previously_surfaced_concern_ids") or [])
    )

    active_total = len(register.get("active") or []) + len(register.get("addressing") or [])
    print(f"\n[1/3] Active concerns: {active_total}")
    print(f"      Previously surfaced: {len(previously_surfaced)}")
    print(f"      Pending questions (latest tick): {len(pending_questions)}")

    # 2. Render
    digest_text = render_digest(
        register=register,
        previously_surfaced_ids=previously_surfaced,
        pending_questions=pending_questions,
    )

    print("\n[2/3] Rendered digest:\n")
    print("─" * 70)
    print(digest_text)
    print("─" * 70)

    if args.dry_run:
        print("\n--dry-run set — not writing or posting.")
        return

    # 3. Write to file
    today_local = get_local_time().strftime("%Y-%m-%d")
    digest_dir = get_repo_root() / _DIGEST_DIR_REL
    digest_dir.mkdir(parents=True, exist_ok=True)
    digest_path = digest_dir / f"digest_{today_local}.md"
    digest_path.write_text(digest_text, encoding="utf-8")
    print(f"\n[3a] Wrote digest → {digest_path}")

    # 4. Persist to unified_log_2026 so the digest shows up in chat history
    #    next time the user loads master_room. This is the durable surface;
    #    works whether or not Flask/socketio is currently running.
    if not args.no_post:
        log_ok = _persist_digest_to_unified_log(
            digest_text=digest_text,
            room_id=args.room_id,
            digest_date=today_local,
        )
        print(f"[3b] unified_log_2026 write (room={args.room_id}): {'OK' if log_ok else 'FAILED'}")

        # ALSO try OutboundChatPublisher — when this runs inside live Emi
        # (e.g. as a routine), it pushes to live socketio subscribers for
        # immediate display. When CLI-only, the publisher is unavailable
        # or has no subscribers; harmless either way.
        try:
            publisher = DI.outbound_chat_publisher
        except AttributeError:
            publisher = None
        if publisher is not None:
            ok = publisher.publish(
                sender="Subconscious",
                text=digest_text,
                reply_to={"type": "socketio", "room_id": args.room_id},
                request_id=f"subconscious_digest_{today_local}",
                sub_data_type=["subconscious_digest"],
            )
            print(f"[3c] Live socketio push: {'OK' if ok else 'no-op (no subscriber)'}")
        else:
            print("[3c] (publisher unavailable in CLI DI — live push skipped)")
    else:
        print("[3b/3c] --no-post set — skipping chat history write + live push.")

    # 5. Update digest state with all currently-active concern IDs
    newly_surfaced_ids = set()
    for bucket in ("active", "addressing"):
        for c in register.get(bucket, []) or []:
            cid = c.get("concern_id")
            if cid:
                newly_surfaced_ids.add(cid)

    state["last_digest_at_utc"] = datetime.now(timezone.utc).isoformat()
    state["previously_surfaced_concern_ids"] = sorted(
        previously_surfaced | newly_surfaced_ids
    )
    state["history_count"] = int(state.get("history_count") or 0) + 1
    save_digest_state(state)

    print(f"\n[4] State saved. previously_surfaced count: {len(state['previously_surfaced_concern_ids'])}")
    print("\n" + "=" * 70)
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    main()
