"""Active windows for routine triggers.

A window declares "this routine is allowed to run inside this period."
Composes with any time policy: interval / daily / weekly all check the
window first, then their normal scheduling rule. If outside the window,
the routine is skipped this tick.

Windows can be declared inline on a routine entry:

    "trigger": {
      "type": "time",
      "policy": { "type": "interval", "min_interval_seconds": 180 },
      "active_window": { "from": "22:00", "to": "08:00", "local": true }
    }

Or by name, referencing an entry in `configs/windows.json`:

    "trigger": {
      "type": "time",
      "policy": { "type": "interval", "min_interval_seconds": 180 },
      "active_window": "sleep"
    }

Window shape:
  - from: "HH:MM" string, local-time start (inclusive)
  - to:   "HH:MM" string, local-time end (exclusive)
  - local: bool, default True. When True, from/to are interpreted in
           the user's local timezone. When False, treated as UTC.
  - weekdays_only: bool, default False. When True, the window is also
           gated to Monday-Friday.

Wraps midnight automatically: a window with from > to (e.g.
"22:00" / "08:00") spans the night.
"""
from __future__ import annotations

import json
from datetime import datetime, time as _time
from pathlib import Path
from typing import Any, Dict, Optional, Union

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir
from app.assistant.utils.time_utils import utc_to_local

logger = get_logger(__name__)

_WINDOWS_FILENAME = "windows.json"
_cache: Optional[Dict[str, Dict[str, Any]]] = None


def _load_named_windows(*, force_reload: bool = False) -> Dict[str, Dict[str, Any]]:
    """Lazy-load configs/windows.json. Missing file = no named windows."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache
    path = Path(get_configs_dir()) / _WINDOWS_FILENAME
    if not path.exists():
        _cache = {}
        return _cache
    raw = json.loads(path.read_text(encoding="utf-8"))
    windows = raw.get("windows") or {}
    if not isinstance(windows, dict):
        raise ValueError(
            f"{_WINDOWS_FILENAME}: 'windows' must be an object keyed by name"
        )
    _cache = {str(k): v for k, v in windows.items() if isinstance(v, dict)}
    return _cache


def resolve_window(window_spec: Union[str, Dict[str, Any], None]) -> Optional[Dict[str, Any]]:
    """Turn a string name or inline dict into a window definition.

    Returns None when window_spec is missing/empty (caller treats as
    "no window restriction"). Raises ValueError for an unknown name or
    a malformed inline window — fail loud, since these are config errors.
    """
    if not window_spec:
        return None
    if isinstance(window_spec, str):
        name = window_spec.strip()
        if not name:
            return None
        named = _load_named_windows()
        if name not in named:
            raise ValueError(
                f"unknown window name {name!r}; declare it in configs/{_WINDOWS_FILENAME}"
            )
        return named[name]
    if isinstance(window_spec, dict):
        if "from" not in window_spec or "to" not in window_spec:
            raise ValueError(
                f"inline window missing 'from'/'to': {window_spec!r}"
            )
        return window_spec
    raise ValueError(
        f"window_spec must be a name (string) or inline dict, got {type(window_spec).__name__}"
    )


def is_in_window(window: Dict[str, Any], now_utc: datetime) -> bool:
    """True if `now_utc` falls inside the resolved window."""
    if not window:
        return True

    weekdays_only = bool(window.get("weekdays_only", False))
    use_local = bool(window.get("local", True))

    when = utc_to_local(now_utc) if use_local else now_utc

    if weekdays_only and when.weekday() >= 5:
        return False

    try:
        start = _parse_hhmm(str(window["from"]))
        end = _parse_hhmm(str(window["to"]))
    except (KeyError, ValueError) as e:
        raise ValueError(f"window has invalid from/to: {window!r}") from e

    now_t = _time(when.hour, when.minute, when.second)

    if start == end:
        # Degenerate window: zero-length, never matches.
        return False
    if start < end:
        # Same-day window: 09:00..17:00 → in if 09:00 <= now < 17:00
        return start <= now_t < end
    # Wraps midnight: 22:00..08:00 → in if now >= 22:00 OR now < 08:00
    return now_t >= start or now_t < end


def _parse_hhmm(s: str) -> _time:
    parts = s.strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"expected HH:MM, got {s!r}")
    h = int(parts[0])
    m = int(parts[1])
    if not (0 <= h <= 23) or not (0 <= m <= 59):
        raise ValueError(f"hour/minute out of range: {s!r}")
    return _time(h, m)
