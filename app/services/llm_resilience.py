"""Single source of truth for LLM transient-failure classification + bounded retry.

Reliability spine, layer R2. The line (per the project's law — fail loud, never silently substitute a
default): only a genuine TRANSIENT infra hiccup earns a bounded backoff-retry — a connection/network
blip, HTTP 429 rate-limit, 502/503/504, Anthropic 529 "overloaded", or SQLite "database is locked".
Everything else fails loud immediately:

  - quota / billing  -> a separate fatal circuit-breaker owns it (retrying would burn the batch);
  - validation / parse errors -> a separate single re-ask owns those (see call_structured_output);
  - auth / config / value errors -> caller bug, never retry.

Timeouts are intentionally NOT retried here. Each provider owns its own timeout budget (OpenAI's
fast/medium/full ladder; Gemini + Anthropic per-call SDK timeouts) and then fails loud — retrying a
full-timeout call would multiply minutes of wall-clock for no gain. This module is the ONE place that
decides "is this worth retrying"; callers must not re-implement the judgement.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Substrings (matched case-insensitively against str(exc)) that mark a retryable transient failure.
# Providers wrap their errors differently, so we classify on the message rather than the type.
_TRANSIENT_MARKERS = (
    "connection reset", "connection aborted", "connection error", "connection refused",
    "reset by peer", "forcibly closed",  # WinError 10054 reset message has neither "connection reset"
    "temporarily unavailable", "service unavailable", "bad gateway", "gateway timeout",
    "rate limit", "rate_limit", "too many requests", "429",
    "502", "503", "504", "529", "overloaded",
    "database is locked",
)

# Substrings that must NEVER be retried, even if a transient marker also appears. Checked first so a
# "429 + quota" billing error (quota, fatal) is not mistaken for a plain rate-limit (transient).
_NON_TRANSIENT_MARKERS = (
    "insufficient_quota", "exceeded your current quota", "quota",
    "invalid api key", "unauthorized", "permission denied",
)


def is_transient(exc: Exception) -> bool:
    """True if *exc* is a transient infra failure worth a bounded retry (see module docstring)."""
    s = str(exc).lower()
    if any(m in s for m in _NON_TRANSIENT_MARKERS):
        return False
    if any(m in s for m in _TRANSIENT_MARKERS):
        return True
    # A bare connection error with no telling message (socket/httpx) is still transient.
    return isinstance(exc, ConnectionError)


def retry_transient(
    fn: Callable[[], Any],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    label: str = "",
) -> Any:
    """Run ``fn()``; on a transient infra failure, back off and retry up to ``attempts`` times total.

    A non-transient exception propagates immediately (fail loud). The final transient failure, after
    the attempts are exhausted, also propagates (fail loud) — retry buys resilience, never silence.
    """
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if not is_transient(exc) or i >= attempts:
                raise
            last = exc
            delay = min(max_delay, base_delay * (2 ** (i - 1)))
            logger.warning(
                "transient LLM failure%s (attempt %d/%d): %s — retrying in %.1fs",
                f" [{label}]" if label else "", i, attempts, exc, delay,
            )
            time.sleep(delay)
    # The loop always returns or raises; this only guards against a future edit to the range.
    if last is not None:
        raise last
    raise RuntimeError("retry_transient: no attempts were made")
