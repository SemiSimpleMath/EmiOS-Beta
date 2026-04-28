"""
System Activity Tracker
=======================
Detects user activity (keyboard/mouse) at the OS level.
Works on Windows using GetLastInputInfo API.
"""

import ctypes
import platform
from datetime import datetime, timezone
from typing import Dict, Any

def get_idle_seconds() -> float | None:
    """
    Returns seconds since last keyboard/mouse input (system-wide).
    Works on Windows (GetLastInputInfo) and Linux (xprintidle).
    Returns None if unavailable.
    """
    system = platform.system()

    if system == 'Windows':
        return _get_idle_seconds_windows()
    elif system == 'Linux':
        return _get_idle_seconds_linux()
    return None


def _get_idle_seconds_windows() -> float | None:
    """Windows idle detection via GetLastInputInfo API."""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)

        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return None

        current_tick = ctypes.windll.kernel32.GetTickCount()
        millis = (current_tick - lii.dwTime) & 0xFFFFFFFF

        max_reasonable_idle_ms = 30 * 24 * 60 * 60 * 1000
        if millis < 0 or millis > max_reasonable_idle_ms:
            return None

        return millis / 1000.0
    except Exception:
        return None


def _get_idle_seconds_linux() -> float | None:
    """Linux idle detection via xprintidle (install: sudo apt install xprintidle)."""
    import subprocess
    try:
        result = subprocess.run(
            ["xprintidle"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        millis = int(result.stdout.strip())
        return millis / 1000.0
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        return None


def get_activity_status() -> Dict[str, Any]:
    """
    Returns a dict with user activity information.
    
    Note: The actual AFK threshold is configured in configs/sleep_tracking.yaml
    under afk_thresholds.confirmed_afk_minutes. The 'status' field here is
    just informational - the AFKMonitor uses the configured threshold directly.
    """
    idle_seconds = get_idle_seconds()
    if idle_seconds is None:
        return {
            "idle_seconds": None,
            "idle_minutes": None,
            "status": "unknown",
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }

    idle_minutes = idle_seconds / 60.0
    
    # Informational status (not used for AFK detection - that uses config)
    if idle_minutes < 1:
        status = "active"
    elif idle_minutes < 5:
        status = "recent"
    elif idle_minutes < 15:
        status = "idle"
    else:
        status = "away"
    
    return {
        "idle_seconds": round(idle_seconds, 1),
        "idle_minutes": round(idle_minutes, 1),
        "status": status,  # informational only
        "last_checked": datetime.now(timezone.utc).isoformat()
    }

