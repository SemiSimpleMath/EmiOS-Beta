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
from typing import Any, Callable, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TransientRateLimit(RuntimeError):
    """A rate-WINDOW quota — requests/tokens per minute. Transient by
    definition: the window clears on its own, so the bounded retry ladder
    owns it. Raised at the provider boundary from the SDK's typed markers
    (OpenAI error.code == "rate_limit_exceeded"; Gemini QuotaFailure with a
    per-minute quotaId). ``retry_after`` carries the provider-suggested wait
    (OpenAI Retry-After header / Gemini RetryInfo.retryDelay) when known.
    """

    def __init__(self, message: str = "", *, retry_after: Optional[float] = None):
        super().__init__(message or "LLM rate limit (per-minute window)")
        self.retry_after = retry_after


class BillingQuotaExhausted(RuntimeError):
    """True billing / daily-quota exhaustion — every subsequent call will
    also fail, so continuing lets batch loops (KG pipeline, entity cards)
    race through items marking work attempted with no results. The interface
    layer stops the world on this type. Raised at the provider boundary from
    the SDK's typed markers (OpenAI error.code == "insufficient_quota";
    Gemini 429 RESOURCE_EXHAUSTED without a per-minute violation).
    """

    def __init__(self, provider: str = "", message: str = ""):
        self.provider = provider
        super().__init__(message or f"LLM billing quota exhausted (provider={provider!r})")


# Rate-window wording — a quota-shaped message carrying one of these names a
# per-minute window, not billing.
_RATE_WINDOW_MARKERS = (
    "per minute", "perminute", "per_minute", "per-minute",
    "requests per min", "tokens per min", "rate_limit_exceeded",
)

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

# Substrings that must NEVER be retried. Quota-shaped messages are owned by
# classify_quota (checked before these), so this list carries only the
# caller-bug / auth family.
_NON_TRANSIENT_MARKERS = (
    "invalid api key", "unauthorized", "permission denied",
)


def classify_quota(exc: Exception) -> Optional[str]:
    """Classify a quota-shaped failure: "billing" | "rate" | None (not quota).

    The ONE quota judgement, used by both the retry ladder (rate → retry)
    and the interface kill switch (billing → stop the world). Types come
    first — the provider boundary translates the SDK's structured markers
    into BillingQuotaExhausted / TransientRateLimit — so the string branch
    below only sees errors that arrived unstructured. That branch is
    conservative by policy: a quota mention with no rate-window wording
    classifies as billing — the failure mode of misclassification must be
    "stopped unnecessarily", never "kept burning the batch".
    """
    if isinstance(exc, BillingQuotaExhausted):
        return "billing"
    if isinstance(exc, TransientRateLimit):
        return "rate"
    s = str(exc).lower()
    if "quota" not in s:
        return None
    if any(m in s for m in _RATE_WINDOW_MARKERS):
        return "rate"
    return "billing"


def is_transient(exc: Exception) -> bool:
    """True if *exc* is a transient infra failure worth a bounded retry (see module docstring)."""
    quota_kind = classify_quota(exc)
    if quota_kind == "billing":
        return False  # fatal — the circuit breaker owns it
    if quota_kind == "rate":
        return True   # per-minute window — clears on its own
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
            # A provider-suggested wait (Retry-After / RetryInfo.retryDelay)
            # beats the computed backoff — capped so a pathological hint
            # can't stall the caller for minutes.
            suggested = getattr(exc, "retry_after", None)
            if suggested:
                try:
                    delay = max(delay, min(float(suggested), 60.0))
                except (TypeError, ValueError):
                    pass
            logger.warning(
                "transient LLM failure%s (attempt %d/%d): %s — retrying in %.1fs",
                f" [{label}]" if label else "", i, attempts, exc, delay,
            )
            time.sleep(delay)
    # The loop always returns or raises; this only guards against a future edit to the range.
    if last is not None:
        raise last
    raise RuntimeError("retry_transient: no attempts were made")
