from __future__ import annotations

import ctypes
import os
import uuid
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.routine_manager.utils import read_json_file, status_dir, write_json_file
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)

CONTROL_RESOURCE_FILENAME = "resource_screen_capture_control.json"


def _repo_root() -> Path:
    return get_repo_root()


def _uploads_temp_dir() -> Path:
    from app.assistant.utils.path_utils import get_uploads_dir
    return get_uploads_dir() / "temp"


def _env_truthy(name: str) -> bool:
    val = os.environ.get(name)
    if val is None:
        return False
    return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}


def _time_between_local(now_local: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    """
    Returns True if local time is within [start, end) for same-day windows.

    For Emi's safety gating we only use same-day windows (e.g. 08:00-17:30).
    """
    try:
        sh, sm = [int(x) for x in start_hhmm.split(":", 1)]
        eh, em = [int(x) for x in end_hhmm.split(":", 1)]
        start_t = time(hour=sh, minute=sm)
        end_t = time(hour=eh, minute=em)
    except Exception:
        # If misconfigured, fail safe (treat as "in blocked window")
        return True

    t = now_local.timetz().replace(tzinfo=None)
    if start_t <= end_t:
        return start_t <= t < end_t
    # Cross-midnight windows (not expected here); handle anyway.
    return t >= start_t or t < end_t


def _get_afk_flags_best_effort() -> tuple[Optional[bool], Optional[bool]]:
    """
    Returns (is_afk, is_potentially_afk). If unknown, returns (None, None).

    Fail-safe defaults are enforced by caller (unknown -> blocked unless explicitly allowed).
    """
    try:
        from app.assistant.ServiceLocator.service_locator import DI

        mon = getattr(DI, "afk_monitor", None)
        if mon is None:
            return None, None
        snapshot = mon.get_computer_activity() or {}
        if not isinstance(snapshot, dict) or not snapshot:
            return None, None
        is_afk = snapshot.get("is_afk")
        is_potentially_afk = snapshot.get("is_potentially_afk")
        if is_afk is None and is_potentially_afk is None:
            return None, None
        return bool(is_afk), bool(is_potentially_afk)
    except Exception:
        return None, None


def _control_path() -> Path:
    return status_dir() / CONTROL_RESOURCE_FILENAME


def _default_control() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "enabled": False,
        "enabled_at_utc": None,
        "enabled_by": None,
        "disabled_at_utc": None,
        "disabled_by": None,
        "disabled_reason": None,
        # Last local date (YYYY-MM-DD) when we auto-disabled at 08:30.
        "auto_disabled_date_local": None,
    }


def _load_control() -> Dict[str, Any]:
    data = read_json_file(_control_path()) or {}
    if not isinstance(data, dict):
        data = {}
    merged = _default_control()
    merged.update({k: v for k, v in data.items() if k in merged or k in ("schema_version",)})
    # Keep unknown keys too (forward-compatible) but ensure required defaults exist.
    for k, v in data.items():
        if k not in merged:
            merged[k] = v
    return merged


def _auto_disable_at_0830_local_if_needed(control: Dict[str, Any], now_local: datetime) -> Dict[str, Any]:
    """
    If enabled and local time is >= 08:30, auto-disable once per local day.
    """
    try:
        if not bool(control.get("enabled", False)):
            return control
        today = now_local.date().isoformat()
        last = control.get("auto_disabled_date_local")
        if last == today:
            return control
        if now_local.time() >= time(8, 30):
            control = dict(control)
            control["enabled"] = False
            control["disabled_at_utc"] = datetime.now(timezone.utc).isoformat()
            control["disabled_by"] = "system"
            control["disabled_reason"] = "auto_off_08_30_local"
            control["auto_disabled_date_local"] = today
            try:
                write_json_file(_control_path(), control)
            except Exception as e:
                logger.warning("Failed to persist auto-disable control file: %s", e)
    except Exception:
        return control
    return control


class MonitorInfo:
    def __init__(
        self,
        index: int,
        left: int,
        top: int,
        right: int,
        bottom: int,
        device: str,
        display_index: Optional[int] = None,
    ):
        self.index = index
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom
        self.device = device
        self.display_index = display_index

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def bbox(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

def _list_monitors() -> List[MonitorInfo]:
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    monitors: List[MonitorInfo] = []

    def _callback(h_monitor, hdc_monitor, lprc_monitor, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(h_monitor, ctypes.byref(info)):
            return 1
        rc = info.rcMonitor
        device = info.szDevice or ""
        monitors.append(
            MonitorInfo(
                index=len(monitors) + 1,
                left=int(rc.left),
                top=int(rc.top),
                right=int(rc.right),
                bottom=int(rc.bottom),
                device=device,
            )
        )
        return 1

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.POINTER(RECT),
        ctypes.c_double,
    )

    user32.EnumDisplayMonitors(0, 0, MONITORENUMPROC(_callback), 0)

    if not monitors:
        return []

    # Deterministic positional ordering:
    # - sort by on-screen position
    # - assign stable 1..N indices used by task/routine configs
    ordered = sorted(monitors, key=lambda m: (m.left, m.top))
    for i, m in enumerate(ordered, start=1):
        m.index = i
        dev = m.device if isinstance(m.device, str) else ""
        up = dev.upper()
        marker = "DISPLAY"
        pos = up.rfind(marker)
        if pos >= 0:
            start = pos + len(marker)
            end = start
            while end < len(up) and up[end].isdigit():
                end += 1
            if end > start:
                try:
                    m.display_index = int(up[start:end])
                except Exception:
                    m.display_index = None
            else:
                m.display_index = None
        else:
            m.display_index = None
    return ordered


class CaptureMonitorScreenshotTool(BaseTool):
    def __init__(self):
        super().__init__("capture_monitor_screenshot")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        # ------------------------------------------------------------------
        # Fail-safe privacy gating: hard-block screenshots in work hours / AFK
        # ------------------------------------------------------------------
        # This is intentionally enforced at the tool layer so higher-level task
        # gating bugs cannot accidentally cause screen capture during work.
        #
        # Overrides (for explicit debugging only):
        # - EMI_DISABLE_SCREENSHOTS=1                      -> always block
        # - EMI_SCREENSHOTS_ALLOW_WORK_HOURS=1             -> allow 08:00-17:30
        # - EMI_SCREENSHOTS_ALLOW_AFK=1                    -> allow while AFK
        # - EMI_SCREENSHOTS_ALLOW_UNKNOWN_AFK=1            -> allow when AFK status unknown
        if _env_truthy("EMI_DISABLE_SCREENSHOTS"):
            return ToolResult(
                result_type="error",
                content="Screenshot capture is disabled by policy (EMI_DISABLE_SCREENSHOTS=1).",
            )

        now_local = datetime.now().astimezone()

        # Manual toggle: screen capture must be explicitly enabled.
        # Auto turns off at 08:30 local (no auto-rearm).
        control = _load_control()
        control = _auto_disable_at_0830_local_if_needed(control, now_local)
        if not bool(control.get("enabled", False)) and not _env_truthy("EMI_SCREENSHOTS_ALLOW_WHEN_TOGGLE_OFF"):
            return ToolResult(
                result_type="error",
                content=(
                    "Screenshot capture blocked by manual toggle (disarmed). "
                    f"Enable via {CONTROL_RESOURCE_FILENAME} in resources/status/."
                ),
                data={"blocked_reason": "manual_toggle_off"},
            )

        if not _env_truthy("EMI_SCREENSHOTS_ALLOW_WORK_HOURS") and _time_between_local(now_local, "08:00", "17:30"):
            return ToolResult(
                result_type="error",
                content=(
                    "Screenshot capture blocked by policy (work hours 08:00-17:30 local). "
                    f"now_local={now_local.strftime('%Y-%m-%d %H:%M %Z')}"
                ),
                data={"blocked_reason": "work_hours"},
            )

        is_afk, is_potentially_afk = _get_afk_flags_best_effort()
        afk_unknown = is_afk is None and is_potentially_afk is None
        if afk_unknown and not _env_truthy("EMI_SCREENSHOTS_ALLOW_UNKNOWN_AFK"):
            return ToolResult(
                result_type="error",
                content=(
                    "Screenshot capture blocked by policy (AFK status unknown). "
                    "Start AFK monitor or set EMI_SCREENSHOTS_ALLOW_UNKNOWN_AFK=1 for debugging."
                ),
                data={"blocked_reason": "afk_unknown"},
            )
        if (is_afk or is_potentially_afk) and not _env_truthy("EMI_SCREENSHOTS_ALLOW_AFK"):
            return ToolResult(
                result_type="error",
                content=(
                    "Screenshot capture blocked by policy (user is AFK / potentially AFK). "
                    f"is_afk={bool(is_afk)} is_potentially_afk={bool(is_potentially_afk)}"
                ),
                data={"blocked_reason": "afk"},
            )

        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data
        monitor_index = int(args.get("monitor_index") or 1)
        if monitor_index <= 0:
            return ToolResult(result_type="error", content="monitor_index must be >= 1")

        try:
            from PIL import ImageGrab
        except Exception as e:
            return ToolResult(
                result_type="error",
                content=f"Screenshot capture requires Pillow (PIL). Install pillow to use this tool. ({e})",
            )

        monitors = _list_monitors()
        if not monitors:
            return ToolResult(result_type="error", content="No monitors detected for screenshot capture")

        if not (1 <= monitor_index <= len(monitors)):
            available = [int(m.index) for m in monitors]
            return ToolResult(
                result_type="error",
                content=(
                    "monitor_index out of range. "
                    f"Requested={monitor_index}, available_positional_indices={available}"
                ),
                data={"available_monitors": len(monitors), "available_positional_indices": available},
            )

        monitor = monitors[monitor_index - 1]
        bbox = monitor.bbox()

        tmp_dir = _uploads_temp_dir()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"monitor_{monitor_index}_{timestamp}_{uuid.uuid4().hex[:6]}.png"
        path = tmp_dir / filename

        try:
            try:
                image = ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                image = ImageGrab.grab(bbox=bbox)
            image.save(path, format="PNG")
        except Exception as e:
            return ToolResult(result_type="error", content=f"Failed to capture monitor {monitor_index}: {e}")

        attachments = [
            {
                "type": "image",
                "path": str(path),
                "original_filename": filename,
                "content_type": "image/png",
            }
        ]

        return ToolResult(
            result_type="monitor_screenshot",
            content=f"Captured monitor {monitor_index} screenshot: {path}",
            data={
                "monitor_index": monitor_index,
                "resolved_monitor_index": int(monitor.index),
                "resolved_display_index": monitor.display_index,
                "image_path": str(path),
                "bounds": {
                    "left": monitor.left,
                    "top": monitor.top,
                    "right": monitor.right,
                    "bottom": monitor.bottom,
                },
                "size": {"width": monitor.width, "height": monitor.height},
                "device": monitor.device,
                "attachments": attachments,
            },
        )


def get_tool_class():
    return CaptureMonitorScreenshotTool
