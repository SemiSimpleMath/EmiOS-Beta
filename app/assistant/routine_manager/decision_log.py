"""Append-only audit trail for routine execution.

Writes one JSON-line per significant event (fired / succeeded / failed
/ auto_disabled) to ``data/routine_decisions/YYYY-MM-DD.jsonl``. Files
rotate by local date so housekeeping is just rm-old.

Skipped-because-routine-noop reasons (interval not reached, not yet
time, already succeeded today, outside active window) are NOT logged
here — the existing logger already covers them and recording them every
tick would generate ~50k rows/day for 37 routines on a 60s tick. We
DO log "interesting" skips: backoff-after-failure, afk-blocked,
feature-disabled, capacity-reached, already-running.

Best-effort: write failures log a warning but never raise into the
routine runtime.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import utc_to_local

logger = get_logger(__name__)

_DECISION_DIR_NAME = "routine_decisions"
_write_lock = threading.Lock()

# Reasons we treat as "boring noop" — _should_run returning False with
# any of these means "everything is fine, nothing to do this tick."
# Don't persist them; the log file would be flooded.
_BORING_SKIP_REASONS = frozenset({
    "not yet time",
    "already succeeded today",
    "already succeeded this week",
    "interval not reached",
    "outside active window",
    "interval disabled",
    "first run",  # this actually allows running, but defensive
})


def _is_interesting_skip(reason: str) -> bool:
    if not reason:
        return False
    reason_norm = reason.strip().lower()
    if reason_norm in _BORING_SKIP_REASONS:
        return False
    # Backoff / day-of-week mismatch / etc — always log.
    return True


def _decisions_dir() -> Path:
    base = Path(get_repo_root()) / "data" / _DECISION_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _file_for(now_utc: datetime) -> Path:
    # Partition by LOCAL date — easier to reason about "today's decisions"
    # than UTC date, especially for routines that schedule by local time.
    local_date = utc_to_local(now_utc).strftime("%Y-%m-%d")
    return _decisions_dir() / f"{local_date}.jsonl"


def record(
    *,
    routine_id: str,
    event: str,
    reason: str = "",
    run_id: Optional[str] = None,
    duration_s: Optional[float] = None,
    error: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one event row. Best-effort: never raises.

    event values:
      - "fired"          : routine started (from _execute_routine)
      - "succeeded"      : finished successfully
      - "failed"         : finished with error
      - "skipped"        : _should_run returned False with an interesting reason
      - "auto_disabled"  : on_error.then=disable_with_ticket fired
    """
    try:
        now = datetime.now(timezone.utc)
        row: Dict[str, Any] = {
            "ts": now.isoformat(),
            "routine_id": routine_id,
            "event": event,
        }
        if reason:
            row["reason"] = reason
        if run_id:
            row["run_id"] = run_id
        if duration_s is not None:
            row["duration_s"] = round(float(duration_s), 3)
        if error:
            # Cap error text — long tracebacks belong in the main log.
            row["error"] = str(error)[:500]
        if extra:
            for k, v in extra.items():
                if k not in row:
                    row[k] = v

        path = _file_for(now)
        line = json.dumps(row, ensure_ascii=False, default=str)
        with _write_lock:
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
    except Exception as e:
        logger.warning("[routine_decision_log] failed to record %s for %s: %s",
                       event, routine_id, e)


def record_skip_if_interesting(routine_id: str, reason: str) -> None:
    """Convenience wrapper: only record if reason isn't a boring no-op."""
    if _is_interesting_skip(reason):
        record(routine_id=routine_id, event="skipped", reason=reason)


def read_recent(
    *,
    routine_id: Optional[str] = None,
    limit: int = 50,
    days_back: int = 2,
) -> List[Dict[str, Any]]:
    """Read recent decision rows, newest-first. Walks backward from today
    across `days_back` daily files until `limit` rows are collected
    (or files run out).
    """
    out: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    base = _decisions_dir()
    for offset in range(days_back):
        from datetime import timedelta as _td
        day_dt = now - _td(days=offset)
        path = base / f"{utc_to_local(day_dt).strftime('%Y-%m-%d')}.jsonl"
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.warning("[routine_decision_log] failed to read %s: %s", path, e)
            continue
        # Reverse for newest-first within the file
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if routine_id and str(row.get("routine_id") or "") != routine_id:
                continue
            out.append(row)
            if len(out) >= limit:
                return out
    return out
