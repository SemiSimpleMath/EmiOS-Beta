"""Process-wide lock that serializes entity-card generation.

Per the no-parallel-card-generation rule (`feedback_no_parallel_card_generation`),
all card writers must serialize behind one lock. Card generation involves an
LLM call plus a multi-statement DB write; concurrent writers corrupt the
entity_cards / entity_card_index pair under SQLite's write-barrier.

The lock lives in this module so every writer path (admin UI, maintenance
regen, nightly bulk runner) shares the same instance. `generate_and_persist_card`
acquires it via the `card_gen_slot` context manager — callers don't need to
acquire explicitly.

For the admin UI's "busy → 409" UX, callers can pass `blocking=False`; on
contention the slot raises `CardGenSlotBusy` instead of waiting.
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()


class CardGenSlotBusy(RuntimeError):
    """Raised when `card_gen_slot(blocking=False)` finds the slot already held."""


class card_gen_slot:
    """Context manager around the shared card-generation lock.

    Default is blocking — callers serialize. Pass `blocking=False` for the
    admin UI's "refuse with 409" pattern: the context manager raises
    `CardGenSlotBusy` immediately if the slot is held.
    """

    def __init__(self, *, blocking: bool = True) -> None:
        self.blocking = blocking
        self.held = False

    def __enter__(self) -> "card_gen_slot":
        self.held = _LOCK.acquire(blocking=self.blocking)
        if not self.held:
            raise CardGenSlotBusy(
                "card generation slot already held by another caller; "
                "request rejected to enforce single-slot rule"
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.held:
            _LOCK.release()
            self.held = False


def is_card_gen_slot_busy() -> bool:
    """Cheap (racy) status read: True if some caller is currently in the slot.

    Use only for status / UX. Don't gate behavior on this — by the time you
    branch on it, another thread may have entered or exited the slot.
    """
    if _LOCK.acquire(blocking=False):
        _LOCK.release()
        return False
    return True
