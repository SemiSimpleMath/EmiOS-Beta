"""
System Activity Tracker
=======================
Detects user activity (keyboard/mouse) at the OS level.
Works on Windows using GetLastInputInfo API.
"""

import ctypes
import platform
import threading
import time
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


# ---------------------------------------------------------------------------
# Robust input tracking — keystroke heartbeat + de-jittered mouse
# ---------------------------------------------------------------------------
# GetLastInputInfo() collapses keyboard+mouse into ONE "last input" tick, so a
# single jittery mouse event (drift, jiggler, RDP) keeps the user "active". This
# anchors on actual keystrokes (a jittery mouse can't type) PLUS de-jittered
# mouse movement, and keeps GetLastInputInfo as the fallback.
#
# PRIVACY INVARIANT — DO NOT VIOLATE:
#   The keyboard hook callback records ONLY a timestamp. It MUST NEVER read,
#   decode, log, or store the key (the KBDLLHOOKSTRUCT / vkCode in lParam).
#   That is the entire line between "activity heartbeat" and "keylogger". Do not
#   add "just the virtual key for debugging" — if you need to debug, log the
#   COUNT or the TIMESTAMP, never the key. Keep the callback trivial.

# Keystroke + mouse are stored SEPARATELY (not just the max) so failures are
# debuggable — see _classify_idle_source / idle_source in get_activity_status.
_last_keystroke_monotonic = None        # set by the hook callback (time only)
_last_real_mouse_monotonic = None       # set by note_cursor_sample (de-jittered)
_listener_start_monotonic = None        # baseline: assume present at startup
_last_cursor_pos = None                 # (x, y) | None
_prev_move_exceeded = False             # for require_sustained (2 consecutive polls)
_mouse_jitter_px = 25.0
_require_sustained = True
_listener_active = False
_hook_thread = None
_hook_thread_id = None
_kbd_proc_ref = None                    # keep CFUNCTYPE callback alive (GC guard)

_WH_KEYBOARD_LL = 13
_WM_QUIT = 0x0012


def _note_keystroke() -> None:
    # Isolated hot path. Records ONLY the time. NEVER inspect the keystroke.
    global _last_keystroke_monotonic
    _last_keystroke_monotonic = time.monotonic()


def _get_cursor_pos():
    """Current cursor position (Windows). Cheap, no hook. None on failure."""
    try:
        from ctypes import wintypes
        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))
    except Exception:
        pass
    return None


def _keyboard_hook_thread(install_event: "threading.Event") -> None:
    """Daemon thread: install a content-blind WH_KEYBOARD_LL hook and pump
    messages (the hook only fires while this thread runs a message loop)."""
    global _hook_thread_id, _kbd_proc_ref, _listener_active
    handle = None
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        LRESULT = ctypes.c_ssize_t
        # WINFUNCTYPE = __stdcall (required for Win32 hook callbacks).
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        def _proc(nCode, wParam, lParam):
            # PRIVACY: do NOT dereference lParam (KBDLLHOOKSTRUCT). Timestamp only.
            if nCode >= 0:
                _note_keystroke()
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        cb = HOOKPROC(_proc)
        _kbd_proc_ref = cb  # MUST hold a ref or the callback is GC'd -> crash

        handle = user32.SetWindowsHookExW(
            _WH_KEYBOARD_LL, cb, kernel32.GetModuleHandleW(None), 0
        )
        if not handle:
            _listener_active = False
            install_event.set()
            return
        _hook_thread_id = kernel32.GetCurrentThreadId()
        _listener_active = True
        install_event.set()

        # Message pump. GetMessage returns 0 on WM_QUIT (clean stop), -1 on error.
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception:
        _listener_active = False
        try:
            install_event.set()
        except Exception:
            pass
    finally:
        try:
            if handle:
                ctypes.windll.user32.UnhookWindowsHookEx(handle)
        except Exception:
            pass
        _listener_active = False


def start_input_listener(*, mouse_jitter_px: float = 25.0,
                         require_sustained: bool = True,
                         use_robust: bool = True) -> bool:
    """Start the keystroke heartbeat + cursor baseline (Windows only).

    Returns True if the robust listener is active. On non-Windows, if disabled,
    or if the hook fails to install, returns False and callers fall back to
    get_idle_seconds() (today's GetLastInputInfo behavior) — no regression.
    NOTE: for public distribution this should be opt-in (use_robust); a global
    low-level hook is keylogger-CLASS infrastructure and AV may flag it.
    """
    global _mouse_jitter_px, _require_sustained, _listener_start_monotonic
    global _last_cursor_pos, _hook_thread, _last_keystroke_monotonic, _last_real_mouse_monotonic
    if not use_robust or platform.system() != "Windows":
        return False
    if _listener_active:
        return True
    _mouse_jitter_px = float(mouse_jitter_px)
    _require_sustained = bool(require_sustained)
    _listener_start_monotonic = time.monotonic()
    _last_keystroke_monotonic = None
    _last_real_mouse_monotonic = None
    _last_cursor_pos = _get_cursor_pos()
    install_event = threading.Event()
    _hook_thread = threading.Thread(
        target=_keyboard_hook_thread, args=(install_event,),
        name="afk-input-listener", daemon=True,
    )
    _hook_thread.start()
    install_event.wait(timeout=2.0)
    return bool(_listener_active)


def stop_input_listener() -> None:
    """Stop the keystroke listener (posts WM_QUIT to break its message loop)."""
    global _listener_active
    tid = _hook_thread_id
    if tid:
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.PostThreadMessageW.argtypes = [
                wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
            ]
            user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)
        except Exception:
            pass
    _listener_active = False


def note_cursor_sample() -> None:
    """Poll-driven mouse de-jitter — call once per AFK poll (no-op unless the
    robust listener is active). Counts cursor motion as 'real' only when it
    moves >= jitter threshold; with require_sustained, it must exceed on two
    consecutive polls (rejects single jitter spikes). CAVEAT: at a 5s poll with
    require_sustained, mouse-only activity can take up to ~10s to register —
    fine for sleep/wellness gates, but keystrokes update instantly while mouse
    does not. Lower the poll interval or disable require_sustained for snappier
    mouse-only recovery."""
    global _last_real_mouse_monotonic, _last_cursor_pos, _prev_move_exceeded
    if not _listener_active:
        return
    pos = _get_cursor_pos()
    if pos is None:
        return
    if _last_cursor_pos is not None:
        dx = pos[0] - _last_cursor_pos[0]
        dy = pos[1] - _last_cursor_pos[1]
        dist = (dx * dx + dy * dy) ** 0.5
        exceeded = dist >= _mouse_jitter_px
        if exceeded and ((not _require_sustained) or _prev_move_exceeded):
            _last_real_mouse_monotonic = time.monotonic()
        _prev_move_exceeded = exceeded
    _last_cursor_pos = pos


def get_idle_seconds_robust():
    """Seconds since the freshest of {last keystroke, last real mouse, listener
    start}. None when the listener isn't active (caller falls back)."""
    if not _listener_active:
        return None
    now = time.monotonic()
    last = _listener_start_monotonic if _listener_start_monotonic is not None else now
    if _last_keystroke_monotonic is not None and _last_keystroke_monotonic > last:
        last = _last_keystroke_monotonic
    if _last_real_mouse_monotonic is not None and _last_real_mouse_monotonic > last:
        last = _last_real_mouse_monotonic
    return max(0.0, now - last)


def _classify_idle_source():
    """(idle_source, keystroke_idle_seconds, mouse_idle_seconds). idle_source is
    whichever signal is freshest: keyboard | real_mouse | startup."""
    now = time.monotonic()
    ks = (now - _last_keystroke_monotonic) if _last_keystroke_monotonic is not None else None
    ms = (now - _last_real_mouse_monotonic) if _last_real_mouse_monotonic is not None else None
    if ks is not None and (ms is None or ks <= ms):
        src = "keyboard"
    elif ms is not None:
        src = "real_mouse"
    else:
        src = "startup"  # no input observed since the listener started
    return src, (round(ks, 1) if ks is not None else None), (round(ms, 1) if ms is not None else None)


def get_activity_status() -> Dict[str, Any]:
    """
    Returns a dict with user activity information.
    
    Note: The actual AFK threshold is configured in configs/sleep_tracking.yaml
    under afk_thresholds.confirmed_afk_minutes. The 'status' field here is
    just informational - the AFKMonitor uses the configured threshold directly.
    """
    # Always capture the raw GetLastInputInfo idle for side-by-side A/B logging,
    # then prefer the robust keystroke + de-jittered-mouse idle.
    raw_idle = get_idle_seconds()
    robust = get_idle_seconds_robust()
    if robust is not None:
        idle_seconds = robust
        idle_source, ks_idle, ms_idle = _classify_idle_source()
    else:
        idle_seconds = raw_idle
        idle_source, ks_idle, ms_idle = ("fallback", None, None)

    raw_idle_seconds = round(raw_idle, 1) if isinstance(raw_idle, (int, float)) else None

    if idle_seconds is None:
        return {
            "idle_seconds": None,
            "idle_minutes": None,
            "status": "unknown",
            "idle_source": "unknown",
            "raw_idle_seconds": raw_idle_seconds,
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
        "idle_source": idle_source,             # keyboard | real_mouse | startup | fallback
        "keystroke_idle_seconds": ks_idle,      # separate components (debuggability)
        "mouse_idle_seconds": ms_idle,
        "raw_idle_seconds": raw_idle_seconds,   # GetLastInputInfo, for side-by-side A/B
        "last_checked": datetime.now(timezone.utc).isoformat()
    }

