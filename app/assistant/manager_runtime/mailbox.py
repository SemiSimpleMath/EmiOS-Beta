"""Mailbox: typed message bus for delivering messages into a running manager.

A manager runs on its own thread inside ``MultiAgentManager._run_loop``.
Anything outside that thread (an HTTP route, an @mention parser at ingress,
another manager, a periodic monitor) needs a safe way to deliver a message
that the manager will see at its next cycle boundary — never mid-LLM-call.

The mailbox is the channel. Senders post typed messages; the manager drains
them at the top of each cycle and dispatches by ``message_type``.

Built-in message types (interpreted by the manager's drain handler):

  ``agent_inject``    payload = ``{role_or_name: text, ...}``
                      Append ``text`` to the runtime-injection slot of the
                      agent identified by ``role_or_name`` (resolved through
                      the manager's role bindings, then by direct name).
                      The agent renders its own slot during prompt assembly.

  ``blackboard_write`` payload = ``{key: value, ...}``
                      ``blackboard.update_state_value(key, value)`` for each.
                      Useful for setting flags like ``cancelled`` from outside.

New types can be added later (pause / resume / replan / etc.) without changing
the mailbox itself — only the manager's drain handler grows new cases.

Senders own message wrapping. The mailbox stores the payload verbatim. For
``agent_inject`` text, the sender is responsible for any user-facing framing
(``+++++ Latest instruction from user +++ User: <body> +++++``); the manager
only routes.

In-memory only. Per-invocation queues live as long as the process. Stale
messages are dropped at drain time via TTL so a message that arrived after
the manager already exited doesn't get applied to a later same-named-but-
different invocation by accident.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Default TTL: messages older than this are dropped at drain time.
# Tuned long enough that a slow tool call completing doesn't lose a steering
# message; short enough that a message from many minutes ago doesn't quietly
# apply to a subsequent unrelated cycle.
_DEFAULT_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class MailboxMessage:
    """One message destined for a manager invocation.

    ``message_type`` selects the manager's handler. ``payload`` is opaque
    to the mailbox itself; semantics are agreed between sender and the
    manager's drain handler.

    ``from_who`` is informational only — useful for logging and for
    rendering ``+++ from: user +++`` style headers if a sender chooses to.
    """
    message_type: str
    payload: Dict[str, Any]
    posted_at_utc: datetime
    from_who: str = "system"
    metadata: Dict[str, Any] = field(default_factory=dict)


class Mailbox:
    """Per-invocation typed-message inbox. Singleton via ``DI.mailbox``.

    Keyed by ``invocation_id`` (the same id ``ManagerInvoker`` uses in its
    active-invocations registry). Posting to an unknown invocation_id is
    harmless — the queue just lingers and ages out.
    """

    def __init__(self, *, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._queues: Dict[str, Deque[MailboxMessage]] = {}
        self._ttl_seconds = float(ttl_seconds)
        logger.info("✅ Mailbox initialized (ttl=%.1fs).", ttl_seconds)

    def post(
        self,
        *,
        invocation_id: str,
        message_type: str,
        payload: Dict[str, Any],
        from_who: str = "system",
        metadata: Dict[str, Any] | None = None,
    ) -> bool:
        """Drop one message into an invocation's queue.

        Returns True if posted, False on bad input. Does NOT verify the
        invocation is still running (that's the sender's concern).
        """
        invocation_id = (invocation_id or "").strip()
        message_type = (message_type or "").strip()
        if not invocation_id or not message_type:
            return False
        if not isinstance(payload, dict):
            return False
        msg = MailboxMessage(
            message_type=message_type,
            payload=dict(payload),
            posted_at_utc=datetime.now(timezone.utc),
            from_who=str(from_who or "system"),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )
        with self._lock:
            self._queues.setdefault(invocation_id, deque()).append(msg)
        logger.info(
            "[mailbox] posted type=%s to %s payload_keys=%s",
            message_type, invocation_id, list(payload.keys()),
        )
        return True

    def drain(self, invocation_id: str) -> List[MailboxMessage]:
        """Pop all currently-valid messages for an invocation, in posting order.

        Stale messages (older than TTL) are silently dropped here. Returns
        an empty list when there's nothing waiting. Called by the manager
        at the top of each cycle.
        """
        invocation_id = (invocation_id or "").strip()
        if not invocation_id:
            return []
        cutoff_seconds = self._ttl_seconds
        now = datetime.now(timezone.utc)
        out: List[MailboxMessage] = []
        with self._lock:
            queue = self._queues.get(invocation_id)
            if not queue:
                return []
            while queue:
                msg = queue.popleft()
                age = (now - msg.posted_at_utc).total_seconds()
                if age > cutoff_seconds:
                    logger.info(
                        "[mailbox] dropping stale message type=%s "
                        "(age=%.1fs > ttl=%.1fs) for %s",
                        msg.message_type, age, cutoff_seconds, invocation_id,
                    )
                    continue
                out.append(msg)
            if not queue:
                self._queues.pop(invocation_id, None)
        return out

    def peek_count(self, invocation_id: str) -> int:
        """Return how many messages are queued (no TTL pruning)."""
        with self._lock:
            queue = self._queues.get(invocation_id)
            return len(queue) if queue else 0

    def clear(self, invocation_id: str) -> None:
        """Drop all queued messages for an invocation (e.g., on cancel)."""
        with self._lock:
            self._queues.pop(invocation_id, None)
