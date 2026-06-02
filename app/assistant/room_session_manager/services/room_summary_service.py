"""
room_summary_service.py — background, per-room chat history compaction service.

One background thread per room_id.  Uses a non-blocking per-room lock (same
pattern as context_engine/pipeline.py) to guarantee at most one summarization
run is in flight for any given room at a time.  Excess triggers are silently
dropped; they are not queued, because by the time the current run finishes the
state has already advanced.
"""

import threading
from typing import Dict

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Which summary agent runs for a room is no longer decided here — the room
# declares it in its own config (ROOM.md policy.chat_compaction.summary_agent).
# The caller resolves that and passes `summary_agent` in. See
# room_policy_service.resolve_room_chat_compaction and the trigger in
# room_session_manager._maybe_trigger_room_summary.

_room_locks: Dict[str, threading.Lock] = {}
_room_locks_guard = threading.Lock()


def _get_room_lock(room_id: str) -> threading.Lock:
    with _room_locks_guard:
        if room_id not in _room_locks:
            _room_locks[room_id] = threading.Lock()
        return _room_locks[room_id]


def maybe_trigger_room_summary(room_id: str, summary_agent: str) -> bool:
    """
    Fire a background summarization run for `room_id` using `summary_agent`,
    if one is not already in progress.

    `summary_agent` is the agent the room opted into (room-specific or the
    generic `room_summary`); the caller reads it from the room's policy.

    Returns True if a background thread was started, False if one was already
    running (the trigger is dropped, not queued).

    Raises on any unexpected error during thread setup (not during the
    background run itself — those are logged as errors inside the thread).
    """
    if not isinstance(room_id, str) or not room_id.strip():
        raise ValueError("maybe_trigger_room_summary requires a non-empty room_id.")
    if not isinstance(summary_agent, str) or not summary_agent.strip():
        raise ValueError("maybe_trigger_room_summary requires a non-empty summary_agent.")

    room_id = room_id.strip()
    agent_name = summary_agent.strip()
    lock = _get_room_lock(room_id)

    if not lock.acquire(blocking=False):
        logger.debug(
            "room_summary_service: summarization already running for room_id=%r — skipping",
            room_id,
        )
        return False

    def _background() -> None:
        try:
            from app.assistant.room_session_manager.services.room_chat_summary import RoomChatSummaryRunner

            runner = RoomChatSummaryRunner(room_id=room_id, agent_name=agent_name)
            produced = runner.run()
            logger.debug(
                "room_summary_service: background run finished room_id=%r agent=%s produced=%s",
                room_id,
                agent_name,
                produced,
            )
        except Exception as e:
            logger.error(
                "room_summary_service: background run failed room_id=%r: %s",
                room_id,
                e,
                exc_info=True,
            )
            raise
        finally:
            lock.release()

    t = threading.Thread(
        target=_background,
        name=f"room_summary_{room_id}",
        daemon=True,
    )
    t.start()
    logger.debug("room_summary_service: background thread started for room_id=%r", room_id)
    return True
