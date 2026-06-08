from __future__ import annotations

from datetime import datetime, timezone, timedelta
import re
from zoneinfo import ZoneInfo
from typing import Any, Union

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

import os

DEFAULT_LOCAL_TZ = "America/Los_Angeles"

# Process-wide config; reads TIMEZONE env var (set in .env) with US Pacific fallback.
GLOBAL_CONFIG = {
    "local_timezone": os.environ.get("TIMEZONE", "").strip() or DEFAULT_LOCAL_TZ
}


def get_local_timezone() -> ZoneInfo:
    """
    Returns the configured local timezone.
    Falls back to UTC if misconfigured.
    """
    tz_name = GLOBAL_CONFIG.get("local_timezone") or DEFAULT_LOCAL_TZ
    try:
        return ZoneInfo(tz_name)
    except Exception as e:
        logger.error(f"Invalid timezone '{tz_name}', falling back to UTC: {e}")
        return ZoneInfo("UTC")


def _parse_iso_like(value: str) -> datetime:
    """
    Parses an ISO 8601 like string into a datetime.

    Supports:
    - Full timestamps with offset.
    - Naive timestamps.
    - 'Z' suffix for UTC.
    - '24:00:00' (midnight of next day - common LLM output)

    WARNING — lossy heuristic: trailing timezone *abbreviations*
    ("PST", "PDT", "EST", etc.) are stripped before parsing. The
    resulting datetime is naive and the abbreviation's offset is
    lost — a downstream parse_iso_utc/parse_iso_local then assigns
    UTC or local based on its own contract, which can silently
    shift the value by hours. Acceptable for LLM output (where
    abbreviations are inconsistent and the prompt usually pins a
    canonical tz upstream); risky for any other input source. If
    you need exact preservation, parse the offset explicitly
    before calling here.
    """
    text = value.strip()
    # Normalize common LLM local-time strings like "YYYY-MM-DD HH:MM:SS PST"
    # Strip trailing timezone abbreviations (PST, PDT, EST, etc.).
    text = re.sub(r"\s+[A-Z]{2,5}$", "", text)
    # Convert "YYYY-MM-DD HH:MM[:SS]" -> "YYYY-MM-DDTHH:MM[:SS]"
    text = re.sub(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)$", r"\1T\2", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    
    # Handle 24:00:00 (ISO 8601 allows this, but Python doesn't)
    # Convert to 00:00:00 of the next day
    if "T24:00" in text or "T24:00:00" in text:
        # Split date and time, add one day to date, set time to 00:00:00
        date_part = text.split("T")[0]
        try:
            base_date = datetime.fromisoformat(date_part)
            next_day = base_date + timedelta(days=1)
            text = next_day.strftime("%Y-%m-%dT00:00:00")
        except Exception as e:
            logger.debug(f"Failed to normalize 24:00 timestamp date_part='{date_part}': {e}", exc_info=True)
            # Fall through to normal parsing which will error
    
    try:
        return datetime.fromisoformat(text)
    except Exception as e:
        raise ValueError(f"Invalid datetime string: {value}") from e


def local_to_utc(local_time: Union[str, datetime]) -> datetime:
    """
    Converts a local time (string or datetime) to an aware UTC datetime.

    Behavior:
    - If input is a naive datetime, assume configured local timezone.
    - If input is aware, respect its timezone.
    - If input is a string, parse as ISO; if naive result, assume local timezone.
    """
    local_tz = get_local_timezone()

    if isinstance(local_time, datetime):
        dt = local_time
    elif isinstance(local_time, str):
        dt = _parse_iso_like(local_time)
    else:
        raise TypeError(f"Unsupported type for local_to_utc: {type(local_time)}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=local_tz)

    return dt.astimezone(timezone.utc)


def utc_to_local(utc_time: Union[str, datetime]) -> datetime:
    """
    Converts a UTC time (string or datetime) to an aware local datetime.

    Behavior:
    - If input is a naive datetime, assume UTC.
    - If input is aware, respect its timezone.
    - If input is a string, parse as ISO; if naive result, assume UTC.
    """
    local_tz = get_local_timezone()

    if isinstance(utc_time, datetime):
        dt = utc_time
    elif isinstance(utc_time, str):
        dt = _parse_iso_like(utc_time)
    else:
        raise TypeError(f"Unsupported type for utc_to_local: {type(utc_time)}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(local_tz)


def update_local_timezone(new_timezone: str) -> None:
    """
    Updates the local timezone used by helpers.

    Raises if the timezone is invalid.
    """
    try:
        ZoneInfo(new_timezone)
    except Exception as e:
        raise ValueError(f"Invalid timezone: {new_timezone}") from e

    GLOBAL_CONFIG["local_timezone"] = new_timezone
    logger.info(f"Local timezone updated to {new_timezone}")


def parse_iso_local(value: Union[str, datetime]) -> datetime:
    """
    Parses a time input and returns an aware UTC datetime, treating naive
    input as **local** time (configured ``TIMEZONE``).

    Use this for user/LLM-supplied datetimes where naive input means
    "local wall-clock time." Use :func:`parse_iso_utc` instead when naive
    input means "already UTC" (e.g., DB rows, internal storage).

    - If str: parsed via :func:`_parse_iso_like`. If the result is naive,
      local-tz is attached, then the value is converted to UTC.
    - If datetime: aware → astimezone(UTC); naive → local-tz attached
      then converted to UTC.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = _parse_iso_like(value)
    else:
        raise TypeError(f"Expected str or datetime, got {type(value)}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_local_timezone())

    return dt.astimezone(timezone.utc)


# Backwards-compat alias. Existing callers across ~8 files import the
# legacy name; new code should reach for parse_iso_local for parallel
# naming with parse_iso_utc.
parse_time_string = parse_iso_local


def convert_utc_object_to_local(data: Any) -> Any:
    """
    Recursively converts timestamps in a JSON-like object to local time.

    Behavior:
    - String values that parse as datetimes:
        - If naive, assume UTC.
        - If aware, use their timezone.
        - Returned as ISO strings in local time.
    - datetime values:
        - If naive, assume UTC.
        - If aware, use their timezone.
        - Returned as aware datetime in local time.
    - Other values unchanged.

    This matches the intent of the original implementation.
    """
    local_tz = get_local_timezone()

    def convert(value: Any) -> Any:
        if isinstance(value, str):
            try:
                dt = _parse_iso_like(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(local_tz).isoformat()
            except ValueError:
                return value

        if isinstance(value, datetime):
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(local_tz)

        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items()}

        if isinstance(value, list):
            return [convert(v) for v in value]

        return value

    return convert(data)


def normalize_google_event_times(event: dict) -> dict:
    """
    Ensures 'start.dateTime' and 'end.dateTime' in a Google event are normalized to UTC ISO format.

    Matches original behavior:
    - Uses parse_time_string (naive treated as local, then to UTC).
    - All day events with 'date' are left unchanged.
    - Mutates and returns the same dict.
    """
    try:
        start = event.get("start", {})
        if "dateTime" in start:
            dt_utc = parse_time_string(start["dateTime"])
            start["dateTime"] = dt_utc.isoformat()

        end = event.get("end", {})
        if "dateTime" in end:
            dt_utc = parse_time_string(end["dateTime"])
            end["dateTime"] = dt_utc.isoformat()
    except Exception as e:
        logger.error(f"Failed to normalize Google event times: {e}")

    return event


def parse_iso_utc(value: Any) -> datetime | None:
    """Parse an ISO-ish timestamp string and return a UTC-aware datetime.

    Returns ``None`` for empty/non-string/unparseable input.
    Naive datetimes are assumed UTC.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _parse_iso_like(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_iso_utc_strict(value: Any, *, label: str = "timestamp") -> datetime:
    """Parse an ISO-ish timestamp string and return a UTC-aware datetime.

    Raises ``ValueError`` when *value* is empty, non-string, or unparseable.
    *label* is included in the error message for diagnostics.
    Naive datetimes are assumed UTC.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty ISO timestamp string, got {value!r}.")
    try:
        parsed = _parse_iso_like(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label}: invalid datetime string {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_now() -> datetime:
    """
    Returns current UTC time as an aware datetime.

    Canonical replacement for ``datetime.now(timezone.utc)`` and the
    deprecated ``datetime.utcnow()``. Use this everywhere a naive UTC or
    aware UTC "now" is needed; pair with ``ensure_aware_utc`` when working
    with datetimes from external sources.
    """
    return datetime.now(timezone.utc)


def ensure_aware_utc(dt: datetime) -> datetime:
    """
    Return *dt* as an aware UTC datetime.

    - Naive input is treated as already UTC (gets UTC tzinfo attached).
    - Aware input in any tz is converted to UTC.

    Use this when you have a datetime *object* (not a string) and need a
    canonical aware-UTC value for storage, comparison, or arithmetic.
    For string inputs, use ``parse_iso_utc``. The convention "naive == UTC"
    matches how SQLite/SQLAlchemy DateTime columns round-trip in this
    codebase: writes assume UTC, reads come back naive on non-tz columns.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# AwareUtcDateTime — SQLAlchemy column type that always round-trips aware UTC
# ---------------------------------------------------------------------------
# SQLite + DateTime(timezone=True) is a known footgun: SQLite has no native
# tz storage, so SQLAlchemy stores datetimes as ISO strings and the round-trip
# is fragile. Rows written naively (the default in much of this codebase
# pre-2026-05-07) read back NAIVE even though the column declares
# timezone=True, which then crashes any aware-vs-naive comparison
# downstream.
#
# This TypeDecorator wraps DateTime(timezone=True) and coerces both
# directions: every bind sends aware UTC; every read returns aware UTC.
# Naive inputs/outputs are assumed to already represent UTC.
#
# Replace `DateTime(timezone=True)` with `AwareUtcDateTime` in column
# definitions and the bug class disappears at the boundary, no per-call-site
# `ensure_aware_utc(...)` wrapping needed.
from sqlalchemy.types import DateTime as _SADateTime, TypeDecorator as _TypeDecorator


class AwareUtcDateTime(_TypeDecorator):
    """SQLAlchemy column type that always round-trips aware-UTC datetimes."""

    impl = _SADateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None or not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def get_local_time() -> datetime:
    """
    Returns current local time as aware datetime.
    """
    return datetime.now(get_local_timezone())


def get_local_time_str() -> str:
    """
    Returns current local time as a formatted string for agents.
    """
    local_time = get_local_time()
    return local_time.strftime("%Y-%m-%d %H:%M:%S %Z")


def format_history_local(ts_utc: Union[str, datetime]) -> str:
    """Format a timestamp the way agent-facing chat history shows it: LOCAL time,
    ``%H:%M`` when it's today, else ``%Y-%m-%d %H:%M``.

    Agents reason purely in local time, so this is the single source of truth for
    message-like entries — chat history AND mailbox @-message injections render
    identically through it, so a steering message never looks different from a
    normal turn."""
    local_dt = utc_to_local(ts_utc)
    now_local = utc_to_local(datetime.now(timezone.utc))
    fmt = "%H:%M" if local_dt.date() == now_local.date() else "%Y-%m-%d %H:%M"
    return local_dt.strftime(fmt)


def to_rfc3339_z(dt: datetime) -> str:
    """
    Returns UTC time in RFC 3339 format with Z suffix.

    If dt is naive, treat as UTC.
    """
    if dt.tzinfo is None:
        logger.warning("Naive datetime passed to to_rfc3339_z, assuming UTC.")
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
