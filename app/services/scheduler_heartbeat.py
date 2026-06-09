"""SSOT for background-loop liveness — the data source for the health surface (R4) and the
no-silent-death trigger (R3).

Each scheduler / background loop calls record_tick(component, ok=..., error=...) at its tick boundary.
State is in-memory on purpose: liveness is a "right now" question, and a process restart correctly
resets it (nothing is alive yet). consecutive_errors lets a caller decide a loop has silently broken
and surface a signal; get_all() feeds the health page. This module is the ONE place liveness is
tracked — schedulers must not each invent their own counters.
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class _Heartbeat:
    component: str
    last_tick_utc: Optional[str] = None
    last_ok_utc: Optional[str] = None
    last_status: str = "unknown"          # "ok" | "error" | "unknown"
    consecutive_errors: int = 0
    total_ticks: int = 0
    total_errors: int = 0
    last_error: Optional[str] = None


_HEARTBEATS: Dict[str, _Heartbeat] = {}
_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_tick(component: str, *, ok: bool, error: Optional[BaseException] = None) -> int:
    """Record one tick outcome for *component*; return its new consecutive_errors count.

    A success resets consecutive_errors to 0; an error increments it. The returned count lets the
    caller decide when repeated failures cross a threshold worth surfacing (no-silent-death).
    """
    with _LOCK:
        hb = _HEARTBEATS.get(component)
        if hb is None:
            hb = _Heartbeat(component=component)
            _HEARTBEATS[component] = hb
        now = _now_iso()
        hb.last_tick_utc = now
        hb.total_ticks += 1
        if ok:
            hb.last_status = "ok"
            hb.last_ok_utc = now
            hb.consecutive_errors = 0
            hb.last_error = None
        else:
            hb.last_status = "error"
            hb.consecutive_errors += 1
            hb.total_errors += 1
            hb.last_error = (str(error)[:500] if error is not None else "unknown")
        return hb.consecutive_errors


def get_all() -> Dict[str, Any]:
    """Snapshot of every tracked component (for the health surface)."""
    with _LOCK:
        return {name: asdict(hb) for name, hb in _HEARTBEATS.items()}


def get(component: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        hb = _HEARTBEATS.get(component)
        return asdict(hb) if hb is not None else None


def reset() -> None:
    """Clear all heartbeats. Test-only; production state is process-lifetime."""
    with _LOCK:
        _HEARTBEATS.clear()
