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


def parse_time_string(value: Union[str, datetime]) -> datetime:
    """
    Parses a time input and returns a UTC-aware datetime.

    Matches original behavior:
    - If str:
        - Parse as ISO-like.
        - If naive, assume local timezone, then convert to UTC.
    - If datetime:
        - If naive, assume local timezone, then convert to UTC.
        - If aware, convert to UTC.
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


def to_rfc3339_z(dt: datetime) -> str:
    """
    Returns UTC time in RFC 3339 format with Z suffix.

    If dt is naive, treat as UTC.
    """
    if dt.tzinfo is None:
        logger.warning("Naive datetime passed to to_rfc3339_z, assuming UTC.")
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
