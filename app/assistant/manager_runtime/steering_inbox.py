"""Steering inbox: cross-invocation message channel for live user steering.

When the user types ``@quimby check the savory page`` in master_room, the
@mention parser resolves Quimby → web_manager invocation X and posts the
text into invocation X's inbox. X's planner consumes the inbox at the
start of its next iteration (safe boundary — never mid-LLM-call) and
folds the steering into its next plan.

In-memory only. Inboxes live as long as the process. TTL on individual
entries so a stale message that arrived after the planner already moved
past doesn't get applied later (with a possibly wrong context).

This module is intentionally tiny: a thread-safe dict of FIFO queues,
plus TTL-based pruning at pop time.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Default TTL: how long a steering message stays valid in the inbox.
# Past this, pop drops it silently. Long enough that a slow tool call
# completing doesn't lose user steering; short enough that messages from
# 5 minutes ago don't apply to the current plan iteration.
_DEFAULT_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class SteeringMessage:
    """One steering message. ``from_user`` is informational (logging /
    prompt context); the actual semantic content is ``text``.
    """
    text: str
    posted_at_utc: datetime
    from_user: str = "user"


class SteeringInbox:
    """Per-invocation FIFO inbox for steering messages.

    Singleton — one instance per process; access via DI.steering_inbox.
    Inbox state is keyed by invocation_id (the same id manager_invoker
    uses in its registry).
    """

    def __init__(self, *, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._inboxes: Dict[str, Deque[SteeringMessage]] = {}
        self._ttl_seconds = ttl_seconds
        logger.info(
            "✅ SteeringInbox initialized (ttl=%.1fs).", ttl_seconds,
        )

    def post(self, *, invocation_id: str, text: str, from_user: str = "user") -> bool:
        """Drop a steering message into an invocation's inbox.

        Returns True if posted. Returns False on bad input (empty
        invocation_id or text). Does NOT verify the invocation is still
        running — caller is expected to check that first via
        active_workers. Posting to a no-longer-active invocation is
        harmless (the queue just lingers and ages out via TTL).
        """
        invocation_id = (invocation_id or "").strip()
        text = (text or "").strip()
        if not invocation_id or not text:
            return False
        msg = SteeringMessage(
            text=text,
            posted_at_utc=datetime.now(timezone.utc),
            from_user=str(from_user or "user"),
        )
        with self._lock:
            self._inboxes.setdefault(invocation_id, deque()).append(msg)
        logger.info(
            "[steering_inbox] posted to %s len=%d",
            invocation_id, len(text),
        )
        return True

    def pop_all(self, invocation_id: str) -> List[SteeringMessage]:
        """Drain all currently-valid steering messages for an invocation.

        Returns a list (possibly empty) in posting order. Messages older
        than the TTL are silently dropped here. Caller decides what to
        do with the returned list (typically: thread it into the next
        planner iteration's input).

        Idempotent on empty inbox — returns [] without raising.
        """
        invocation_id = (invocation_id or "").strip()
        if not invocation_id:
            return []
        cutoff_seconds = self._ttl_seconds
        now = datetime.now(timezone.utc)
        out: List[SteeringMessage] = []
        with self._lock:
            queue = self._inboxes.get(invocation_id)
            if not queue:
                return []
            while queue:
                msg = queue.popleft()
                age = (now - msg.posted_at_utc).total_seconds()
                if age > cutoff_seconds:
                    logger.info(
                        "[steering_inbox] dropping stale message "
                        "(age=%.1fs > ttl=%.1fs) for %s",
                        age, cutoff_seconds, invocation_id,
                    )
                    continue
                out.append(msg)
            # Clean up the empty deque entry.
            if not queue:
                self._inboxes.pop(invocation_id, None)
        return out

    def peek_count(self, invocation_id: str) -> int:
        """Return how many messages are currently queued (no TTL pruning).
        Diagnostic only; pop_all is the canonical reader."""
        with self._lock:
            queue = self._inboxes.get(invocation_id)
            return len(queue) if queue else 0

    def clear(self, invocation_id: str) -> None:
        """Drop all queued messages for an invocation (e.g., on cancel)."""
        with self._lock:
            self._inboxes.pop(invocation_id, None)
