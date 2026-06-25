"""monitor_utils.py — enumerate monitors and their pixel geometry.

Windows-only (ctypes, no extra dependency). Use this to put a window on a specific
physical display: target a monitor by absolute pixel coordinates (e.g. Chrome's
--window-position / --window-size), NOT by a monitor "number".

Note: a non-primary monitor can have NEGATIVE coordinates when it sits left of /
above the primary (e.g. a portrait panel at (-1080, 150)).

vertical_monitor() returns the monitor's WORK AREA (full bounds minus the taskbar) —
what you want for a "maximized" window that doesn't hide under the taskbar.

CLI: `python -m app.assistant.utils.monitor_utils` prints the live layout.
"""
from __future__ import annotations

import sys


def _enum_monitors():
    """Return [(full, work), ...] for every monitor, each rect as (x, y, w, h) in
    virtual-desktop pixels. (x, y) is the top-left and may be negative. Windows-only —
    raises on other platforms or on API failure."""
    if sys.platform != "win32":
        raise RuntimeError("monitor_utils is Windows-only")
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    out = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HANDLE, wintypes.HANDLE,
                              ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def _cb(hmon, hdc, lprc, lparam):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        m, w = mi.rcMonitor, mi.rcWork
        out.append(((m.left, m.top, m.right - m.left, m.bottom - m.top),
                    (w.left, w.top, w.right - w.left, w.bottom - w.top)))
        return 1

    if not ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(_cb), 0):
        raise OSError("EnumDisplayMonitors failed")
    return out


def all_monitors():
    """Full-bounds (x, y, w, h) of every monitor."""
    return [full for full, _work in _enum_monitors()]


def vertical_monitor():
    """WORK-AREA (x, y, w, h) of the first portrait (height > width) monitor — the
    usable region minus the taskbar, for a maximized window. None if there isn't one
    or detection is unavailable. Best-effort: positioning is an optional hint, so
    failures degrade to None rather than raising."""
    try:
        for full, work in _enum_monitors():
            if full[3] > full[2]:  # portrait, judged by full bounds
                return work
        return None
    except Exception:
        return None


if __name__ == "__main__":
    for i, (full, work) in enumerate(_enum_monitors()):
        tag = "VERTICAL" if full[3] > full[2] else "horizontal"
        print(f"[{i}] {tag}  full={full}  work={work}")
    print("portrait work-area pick ->", vertical_monitor())
