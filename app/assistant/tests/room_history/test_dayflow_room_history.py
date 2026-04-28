#!/usr/bin/env python3
"""
Test script for RoomHistoryBuilder: what messages the dayflow_orchestrator room
should be getting right now.

Sources: global_blackboard (in-memory) + UnifiedLog2026 (persisted).
Note: dayflow_orchestrator cadence tick uses message_persistence_mode=
"global_blackboard_only", so its messages are NOT in UnifiedLog2026. When run
standalone, global_blackboard is empty — the test shows DB diagnostics and
what the builder would return. Run while the app is active to see blackboard
messages (from a debug route or inline), or after dayflow messages are persisted.

Run from repo root:
  python -m pytest app/assistant/tests/room_history/test_dayflow_room_history.py -v -s
  # or standalone:
  python -m app.assistant.tests.room_history.test_dayflow_room_history
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.assistant.room_session_manager.services.room_history_builder import RoomHistoryBuilder
from app.models.base import get_session


def _bootstrap_minimal() -> None:
    """Register only the services RoomHistoryBuilder needs."""
    if ServiceLocator.get("global_blackboard") is None:
        ServiceLocator.register("global_blackboard", GlobalBlackBoard())


def _db_diagnostics(room_id: str) -> None:
    """Print raw DB counts for room_id to help debug filtering."""
    session = get_session()
    try:
        from sqlalchemy import func

        count_dayflow = (
            session.query(func.count(UnifiedLog2026.id))
            .filter(UnifiedLog2026.room_id == room_id)
            .scalar()
            or 0
        )
        count_any = session.query(func.count(UnifiedLog2026.id)).scalar() or 0
        # Sample room_ids present in DB
        rows = (
            session.query(UnifiedLog2026.room_id, func.count(UnifiedLog2026.id))
            .group_by(UnifiedLog2026.room_id)
            .all()
        )
        session.close()
        print(f"\nDB: unified_log_2026 has {count_any} total rows")
        print(f"DB: rows with room_id={room_id!r}: {count_dayflow}")
        if rows:
            print("DB: room_id distribution:", dict(rows))
    except Exception as e:
        print(f"DB diagnostics error: {e}")
        session.close()


def test_dayflow_orchestrator_room_history() -> None:
    """Build and print what messages dayflow_orchestrator room would get right now."""
    _bootstrap_minimal()

    room_id = "dayflow_orchestrator"
    _db_diagnostics(room_id)

    builder = RoomHistoryBuilder()
    limit = 80

    # Shared rooms as per dayflow_orchestrator/access.json — includes master_room chat
    shared_chat_room_ids = ["master_room"]

    # load_room_history_messages: raw merged list (before day-cutoff, limit, dedup)
    raw_messages = builder.load_room_history_messages(
        room_id=room_id,
        room_surface=None,
        room_context_id=None,
        shared_chat_room_ids=shared_chat_room_ids,
        excluded_room_modes=None,
        max_age_hours=48,
    )

    # build_messages: final prompt-ready list (after day cutoff, limit, dedup)
    final_messages = builder.build_messages(
        room_id=room_id,
        limit=limit,
        room_surface=None,
        room_context_id=None,
        shared_chat_room_ids=shared_chat_room_ids,
        excluded_room_modes=None,
    )

    # build_context: string format as used in prompts
    context_str = builder.build_context(
        room_id=room_id,
        limit=limit,
        room_surface=None,
        room_context_id=None,
        shared_chat_room_ids=shared_chat_room_ids,
        excluded_room_modes=None,
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("\n" + "=" * 70)
    print(f"dayflow_orchestrator room history @ {now}")
    print(f"Scope: shared_chat_room_ids={shared_chat_room_ids} (from access.json)")
    print("=" * 70)
    print(f"\nRaw messages (load_room_history_messages, max_age=48h): {len(raw_messages)}")
    for i, m in enumerate(raw_messages[-20:]):  # last 20 of raw
        ts = getattr(m, "timestamp", None)
        ts_str = ts.strftime("%H:%M") if ts else "?"
        role = getattr(m, "role", "") or ""
        sender = getattr(m, "room_speaker_name", None) or getattr(m, "sender", "") or role
        content = (getattr(m, "content", "") or "")[:60]
        sub = getattr(m, "sub_data_type", []) or []
        print(f"  {i+1:2}. [{ts_str}] {sender}: {content!r}  sub={sub}")

    print(f"\nFinal messages (build_messages, limit={limit}): {len(final_messages)}")
    for i, m in enumerate(final_messages):
        ts = getattr(m, "timestamp", None)
        ts_str = ts.strftime("%H:%M") if ts else "?"
        sender = getattr(m, "room_speaker_name", None) or getattr(m, "sender", "") or getattr(m, "role", "")
        content = (getattr(m, "content", "") or "")[:70]
        print(f"  {i+1:2}. [{ts_str}] {sender}: {content!r}")

    print("\n--- build_context (prompt-ready string) ---")
    print(context_str or "(empty)")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    test_dayflow_orchestrator_room_history()
