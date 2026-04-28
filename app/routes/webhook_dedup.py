"""
Small in-memory idempotency cache for inbound webhooks.

Twilio and Telegram both retry webhooks when the server's response is
non-2xx or slow. If the server processed the message but the response was
lost, the retry arrives with the same message_sid / update_id and we'd
process it twice.

This module tracks recently-seen message IDs per transport and lets the
webhook handler skip duplicates. The cache is bounded — no persistent
store, no Redis. Sufficient for single-user self-hosted deployments.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict


class WebhookDedupCache:
    """Bounded FIFO cache of `{key: first_seen_ts}`.

    Not safe across process restarts — but webhook retries typically happen
    within seconds of the original, so a single-process memory cache covers
    the real failure mode. If you restart Flask between original delivery
    and retry, a duplicate may slip through; that's rare and the downstream
    message_persistence layer usually idempotency-checks on its own.
    """

    def __init__(self, *, max_size: int = 2048, ttl_seconds: float = 600.0) -> None:
        self._max_size = max(100, int(max_size))
        self._ttl = float(ttl_seconds)
        self._entries: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        """Return True if this key was seen recently. Marks the key as seen."""
        if not isinstance(key, str) or not key.strip():
            return False
        key = key.strip()
        now = time.time()
        with self._lock:
            # Evict expired entries.
            cutoff = now - self._ttl
            while self._entries and next(iter(self._entries.values())) < cutoff:
                self._entries.popitem(last=False)
            if key in self._entries:
                # Move to end (refresh LRU order) and report as duplicate.
                self._entries.move_to_end(key)
                return True
            self._entries[key] = now
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)
            return False


_twilio_dedup = WebhookDedupCache()
_telegram_dedup = WebhookDedupCache()
_slack_dedup = WebhookDedupCache()


def is_duplicate_twilio(message_sid: str) -> bool:
    """True if this Twilio MessageSid was already processed in the recent window."""
    return _twilio_dedup.seen(message_sid)


def is_duplicate_telegram(update_id: str | int) -> bool:
    """True if this Telegram update_id was already processed in the recent window."""
    return _telegram_dedup.seen(str(update_id))


def is_duplicate_slack(event_id: str) -> bool:
    """True if this Slack event_id was already processed in the recent window."""
    return _slack_dedup.seen(event_id)
