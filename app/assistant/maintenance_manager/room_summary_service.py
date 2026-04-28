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

# Maps room_id -> agent name. Rooms not listed fall back to the generic agent.
_ROOM_AGENT_REGISTRY: Dict[str, str] = {
    "master_room": "master_room::room_summary",
    "dayflow_orchestrator": "dayflow_orchestrator::room_summary",
}
_DEFAULT_AGENT = "room_summary"

_room_locks: Dict[str, threading.Lock] = {}
_room_locks_guard = threading.Lock()


def _get_room_lock(room_id: str) -> threading.Lock:
    with _room_locks_guard:
        if room_id not in _room_locks:
            _room_locks[room_id] = threading.Lock()
        return _room_locks[room_id]


def maybe_trigger_room_summary(room_id: str) -> bool:
    """
    Fire a background summarization run for `room_id` if one is not already
    in progress.

    Returns True if a background thread was started, False if one was already
    running (the trigger is dropped, not queued).

    Raises on any unexpected error during thread setup (not during the
    background run itself — those are logged as errors inside the thread).
    """
    if not isinstance(room_id, str) or not room_id.strip():
        raise ValueError("maybe_trigger_room_summary requires a non-empty room_id.")

    room_id = room_id.strip()
    lock = _get_room_lock(room_id)

    if not lock.acquire(blocking=False):
        logger.debug(
            "room_summary_service: summarization already running for room_id=%r — skipping",
            room_id,
        )
        return False

    def _background() -> None:
        try:
            from app.assistant.maintenance_manager.room_chat_summary import RoomChatSummaryRunner

            agent_name = _ROOM_AGENT_REGISTRY.get(room_id, _DEFAULT_AGENT)
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
