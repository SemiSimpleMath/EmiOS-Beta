"""LLM call telemetry writer.

Single chokepoint for persisting per-call usage to `llm_call_log`.
Hooks live in the provider strategies (which see the raw API response
with the usage block) — they call `record_llm_call(...)` in a finally
block so failures persist too.

Agent attribution rides a contextvar set at the agent-runtime layer
(`LLMClient.call_structured_output`) so the lower layers don't need
to thread agent_name through every kwarg signature.

Failure-tolerant AND non-blocking: record_llm_call() builds the row
(timestamps + contextvars captured in the calling thread) and enqueues
it — the caller never touches a DB connection, so telemetry can
neither break nor stall the actual LLM call, and can never extend a
write transaction the calling thread holds (the 2026-07-07 lock-storm
amplifier: each telemetry insert burned the 30s busy_timeout inside
the holder's thread). A single daemon thread drains the queue and
lands rows in batches through db_manager once they are absolutely
ready. Rows land moments after the call; a hard process kill loses
whatever is still queued — acceptable for accounting telemetry.
Queue overflow and exhausted flush retries log warnings with counts.

Phase 1: OpenAI provider only. Anthropic + Gemini follow in Phase 2.
"""

import contextvars
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import utc_now

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Agent attribution via contextvar
# --------------------------------------------------------------------------

_current_agent_name: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_current_agent_name", default=None
)
_current_caller_request_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_current_caller_request_id", default=None
)
_current_caller_manager_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_current_caller_manager_id", default=None
)
_current_caller_scope_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "llm_current_caller_scope_id", default=None
)


def set_current_call_context(
    *,
    agent_name: Optional[str] = None,
    caller_request_id: Optional[str] = None,
    caller_manager_id: Optional[str] = None,
    caller_scope_id: Optional[str] = None,
) -> dict:
    """Set call context vars; returns the old values so callers can restore.

    Use via try/finally at the call site:

        prev = set_current_call_context(agent_name=agent.name, ...)
        try:
            result = llm.structured_output(...)
        finally:
            set_current_call_context(**prev)
    """
    prev = {
        "agent_name": _current_agent_name.get(),
        "caller_request_id": _current_caller_request_id.get(),
        "caller_manager_id": _current_caller_manager_id.get(),
        "caller_scope_id": _current_caller_scope_id.get(),
    }
    _current_agent_name.set(agent_name)
    _current_caller_request_id.set(caller_request_id)
    _current_caller_manager_id.set(caller_manager_id)
    _current_caller_scope_id.set(caller_scope_id)
    return prev


def get_current_call_context() -> dict:
    return {
        "agent_name": _current_agent_name.get(),
        "caller_request_id": _current_caller_request_id.get(),
        "caller_manager_id": _current_caller_manager_id.get(),
        "caller_scope_id": _current_caller_scope_id.get(),
    }


# --------------------------------------------------------------------------
# Price table (lazy-loaded)
# --------------------------------------------------------------------------

_price_cache: Optional[dict] = None

# Engines already warned about — one warning per engine per process, so a
# missing price entry is loud without flooding (an unpriced engine can fire
# thousands of calls a day, every one silently costed $0 otherwise; the
# gpt-5.4/gpt-5.5 rows of 2026-05-27/28 shipped exactly that way).
_unpriced_warned: set[str] = set()


def _load_prices() -> dict:
    global _price_cache
    if _price_cache is not None:
        return _price_cache
    path = get_repo_root() / "configs" / "llm_prices.json"
    if not path.exists():
        logger.warning("[llm_call_logger] price table missing at %s", path)
        _price_cache = {}
        return _price_cache
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        _price_cache = {
            k: v for k, v in raw.items()
            if not k.startswith("_") and isinstance(v, dict)
        }
        return _price_cache
    except Exception as e:
        logger.warning("[llm_call_logger] price table parse failed: %s", e)
        _price_cache = {}
        return _price_cache


def _cost_for(
    engine: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> tuple[float, float]:
    """Returns (input_cost_usd, output_cost_usd). 0 / 0 if engine unknown.

    `input_tokens` is the provider's total input count, which INCLUDES
    cached_tokens. Cached portion is billed at the engine's cached_input
    rate when known (OpenAI auto-caches prompts ≥1024 tokens with shared
    prefixes — 10× cheaper). When the engine's cached_input rate isn't
    in the price table, cached tokens fall back to the full input rate
    (conservative — matches old behavior, slight over-count vs reality).
    """
    prices = _load_prices()
    entry = prices.get(engine)
    if not entry:
        if engine not in _unpriced_warned:
            _unpriced_warned.add(engine)
            logger.warning(
                "[llm_call_logger] no price entry for engine %r — cost recorded "
                "as $0; add it to configs/llm_prices.json",
                engine,
            )
        return 0.0, 0.0
    in_rate = float(entry.get("input_per_1m_usd") or 0.0)
    out_rate = float(entry.get("output_per_1m_usd") or 0.0)
    cached_rate_raw = entry.get("cached_input_per_1m_usd")
    cached_rate = float(cached_rate_raw) if cached_rate_raw is not None else in_rate

    cached = max(int(cached_tokens or 0), 0)
    # Guard against cached > input (shouldn't happen, but providers
    # occasionally report quirky numbers); clamp.
    cached = min(cached, max(int(input_tokens or 0), 0))
    non_cached = max(int(input_tokens or 0) - cached, 0)

    in_cost = (non_cached / 1_000_000.0) * in_rate + (cached / 1_000_000.0) * cached_rate
    out_cost = (output_tokens / 1_000_000.0) * out_rate
    return in_cost, out_cost


# --------------------------------------------------------------------------
# Record helper — enqueue in the caller, batch-write in a daemon thread
# --------------------------------------------------------------------------

# ~30 min of storm-rate traffic; beyond that, new rows drop with a warning
# rather than ever blocking an LLM thread.
_QUEUE_MAX = 10_000
_BATCH_MAX = 200
_FLUSH_ATTEMPTS = 3
_FLUSH_RETRY_SLEEP_S = 5.0

_row_queue: "queue.Queue[dict]" = queue.Queue(maxsize=_QUEUE_MAX)
_writer_started = False
_writer_start_lock = threading.Lock()


def record_llm_call(
    *,
    engine: str,
    provider: str,
    usage: Any,
    duration_ms: int,
    status: str = "ok",
) -> None:
    """Enqueue one llm_call_log row. Failure-tolerant, never blocks.

    The row (including ts_utc and the attribution contextvars) is fully
    materialized here in the calling thread; the background writer only
    persists it. This function must stay connection-free — it runs in
    the finally block of every LLM call, including inside threads that
    hold open write transactions.

    `usage` is the raw usage object from the provider response (or None
    when the call failed before the response arrived, or when the
    response didn't include usage). For OpenAI responses-API:
      usage.input_tokens, usage.output_tokens, usage.input_tokens_details.cached_tokens
    For OpenAI chat-completions API:
      usage.prompt_tokens, usage.completion_tokens
    We try both naming conventions.
    """
    try:
        input_tokens, output_tokens, cached_tokens = _extract_usage_counts(usage)
        in_cost, out_cost = _cost_for(engine, input_tokens, output_tokens, cached_tokens)
        ctx = get_current_call_context()

        row = {
            "ts_utc": utc_now(),
            "agent_name": ctx.get("agent_name") or "(unknown)",
            "caller_request_id": ctx.get("caller_request_id"),
            "caller_manager_id": ctx.get("caller_manager_id"),
            "caller_scope_id": ctx.get("caller_scope_id"),
            "engine": engine,
            "provider": provider,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "cached_tokens": int(cached_tokens),
            "input_cost_usd": round(in_cost, 6),
            "output_cost_usd": round(out_cost, 6),
            "total_cost_usd": round(in_cost + out_cost, 6),
            "duration_ms": int(duration_ms),
            "status": status,
        }
        _ensure_writer_thread()
        _row_queue.put_nowait(row)
    except queue.Full:
        logger.warning(
            "[llm_call_logger] telemetry queue full (%d) — dropped llm_call row",
            _QUEUE_MAX,
        )
    except Exception as e:
        # Never break the actual LLM call because telemetry failed.
        logger.warning("[llm_call_logger] failed to record llm_call: %s", e)


def _ensure_writer_thread() -> None:
    global _writer_started
    if _writer_started:
        return
    with _writer_start_lock:
        if _writer_started:
            return
        t = threading.Thread(
            target=_writer_loop, name="llm-call-log-writer", daemon=True
        )
        t.start()
        _writer_started = True


def _writer_loop() -> None:
    """Drain the queue forever, writing rows in batches.

    The loop must survive anything — a dead writer thread would mean
    silent telemetry loss for the rest of the process lifetime.
    """
    while True:
        try:
            rows = [_row_queue.get()]
            try:
                while len(rows) < _BATCH_MAX:
                    rows.append(_row_queue.get_nowait())
            except queue.Empty:
                pass
            _flush_batch(rows)
        except Exception as e:
            logger.warning("[llm_call_logger] writer loop error: %s", e)


def _flush_batch(rows: list) -> None:
    """Write one batch through db_manager (short transaction, rows ready).

    Retries ride out transient write-lock contention: the thread has
    nothing else to do, so waiting here is free and the rows survive.
    """
    from app.models.db_manager import get_db_manager
    from app.models.llm_call_log import LLMCallLog

    for attempt in range(1, _FLUSH_ATTEMPTS + 1):
        try:
            # Fresh ORM objects per attempt — instances from a rolled-back
            # session are not safely re-addable.
            get_db_manager().write_many(
                (LLMCallLog(**r) for r in rows), op="llm_call_logger.flush"
            )
            return
        except Exception as e:
            if attempt == _FLUSH_ATTEMPTS:
                logger.warning(
                    "[llm_call_logger] dropped %d llm_call row(s) after %d flush attempts: %s",
                    len(rows), attempt, e,
                )
            else:
                time.sleep(_FLUSH_RETRY_SLEEP_S)


def _extract_usage_counts(usage: Any) -> tuple[int, int, int]:
    """Returns (input_tokens, output_tokens, cached_tokens). 0/0/0 on missing usage.

    Three provider conventions:
      - OpenAI Responses API: usage.input_tokens / output_tokens, with
        usage.input_tokens_details.cached_tokens for the cached sub-count.
      - OpenAI Chat Completions: usage.prompt_tokens / completion_tokens.
      - Anthropic: usage.input_tokens / output_tokens (matches OpenAI naming).
      - Gemini: usage_metadata.prompt_token_count / candidates_token_count,
        with cached_content_token_count for the cached sub-count.
    Try each in turn; first non-None wins.
    """
    if usage is None:
        return 0, 0, 0
    input_tokens = (
        getattr(usage, "input_tokens", None)
        or getattr(usage, "prompt_tokens", None)
        or getattr(usage, "prompt_token_count", None)
        or 0
    )
    output_tokens = (
        getattr(usage, "output_tokens", None)
        or getattr(usage, "completion_tokens", None)
        or getattr(usage, "candidates_token_count", None)
        or 0
    )
    # Gemini thinking models bill reasoning as output but report it in a SEPARATE
    # field (thoughts_token_count), not in candidates_token_count. Without this we
    # silently undercount output on every gemini-3-flash-preview call. OpenAI nests
    # reasoning inside output_tokens already, so getattr→0 there (no double count).
    output_tokens = (output_tokens or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
    # Cached tokens: OpenAI nests them; Anthropic + Gemini surface a flat field.
    cached = 0
    details = getattr(usage, "input_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    if not cached:
        cached = (
            getattr(usage, "cached_tokens", 0)
            or getattr(usage, "cached_content_token_count", 0)
            or 0
        )
    return int(input_tokens or 0), int(output_tokens or 0), int(cached or 0)
