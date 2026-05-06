"""local_camera_snapshot — pull a frame from any RTSP-capable camera on the LAN.

Use this for cameras that expose RTSP directly (Tapo with RTSP toggled on, Wyze
with custom firmware, Reolink, etc.). Compared to Ring's snapshot endpoint
(640×360 thumbnail, no subscription = no recordings) this gives full sensor
resolution (typically 1080p) on demand with no cloud dependency.

Configuration lives in ``configs/smart_home_tools.json`` under a new
``local_cameras`` section:

    "local_cameras": {
        "enabled": true,
        "devices": [
            {
                "alias": "Bedroom",
                "rtsp_url": "rtsp://user:pass@192.168.1.50:554/stream1",
                "kind": "bedroom",
                "notes": "Tapo C200 over the bed"
            }
        ]
    }

The bridge for this tool is in-process (no HTTP round-trip) — ffmpeg pulls
one frame from the RTSP URL directly. Frames land in
``data/local_camera_snapshots/`` and a ``ring_snapshot_captured`` event fires
so the existing analyzer subscribers (sleep_analyzer / door_bell_analyzer)
pick the frame up automatically. The event payload includes ``kind`` so
subscribers can filter by camera role rather than by raw alias.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_configs_dir, get_repo_root
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


_DEFAULT_TIMEOUT_SECONDS = 15


def _local_cameras_dir() -> Path:
    return get_repo_root() / "data" / "local_camera_snapshots"


def _load_local_cameras() -> list[dict]:
    """Read configs/smart_home_tools.json local_cameras.devices. Empty list if missing."""
    path = get_configs_dir() / "smart_home_tools.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("smart_home_tools.json parse error: %s", e)
        return []
    section = payload.get("local_cameras") if isinstance(payload, dict) else None
    if not isinstance(section, dict):
        return []
    devices = section.get("devices")
    return devices if isinstance(devices, list) else []


def _resolve_camera(alias: str) -> dict:
    """Return the device record matching alias (case-insensitive) or raise."""
    target = str(alias or "").strip().lower()
    if not target:
        raise ValueError("camera_alias is required.")
    for d in _load_local_cameras():
        if not isinstance(d, dict):
            continue
        if str(d.get("alias") or "").strip().lower() == target:
            return d
    raise ValueError(
        f"No local camera configured with alias {alias!r}. "
        "Add one in configs/smart_home_tools.json under local_cameras.devices "
        "with fields: alias, rtsp_url, kind (optional), notes (optional)."
    )


def _safe_filename_part(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(text or ""))[:32]


def _capture_rtsp_frame(rtsp_url: str, out_path: Path, timeout_s: int) -> None:
    """Pull a single frame from the RTSP stream into out_path. Raises on failure."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg not found on PATH. Install ffmpeg "
            "(Windows: https://www.gyan.dev/ffmpeg/builds/  Linux/Mac: package manager)."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",   # TCP is more reliable than UDP for one-shot
        "-i", rtsp_url,
        "-frames:v", "1",
        "-y",                       # overwrite if exists
        str(out_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"ffmpeg timed out after {timeout_s}s pulling RTSP frame. "
            "Check the camera is reachable on the LAN and RTSP is enabled."
        ) from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[-800:]
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}). stderr tail: {stderr}"
        )
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg succeeded but no output file was produced.")


def _publish_snapshot_event(
    *,
    snapshot_path: str,
    filename: str,
    alias: str,
    kind: str,
    captured_at_utc: str,
) -> None:
    """Fire the same event ring_analysis subscribers listen to so sleep_analyzer
    / door_bell_analyzer / etc. pick this frame up without any rewiring.
    Adds a ``kind`` field (e.g. "bedroom", "doorbell") so subscribers can
    filter on role rather than hardcoded camera ids.
    """
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import Message
        msg = Message(
            event_topic="ring_snapshot_captured",  # event name kept for compat
            data={
                "snapshot_path": snapshot_path,
                "filename": filename,
                "camera_id": alias,                # subscribers can match this
                "kind": kind,                      # OR this — preferred
                "source": "local_camera",
                "captured_at_utc": captured_at_utc,
            },
        )
        DI.event_hub.publish(msg)
    except Exception as e:
        logger.warning("camera_snapshot event publish failed: %s", e)


class LocalCameraSnapshotTool(BaseTool):
    requires_approval = False  # read-only

    def __init__(self):
        super().__init__("local_camera_snapshot")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
            arguments = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else {}
            alias = str(arguments.get("camera_alias") or "").strip()
            if not alias:
                raise ValueError("camera_alias is required.")

            device = _resolve_camera(alias)
            rtsp_url = str(device.get("rtsp_url") or "").strip()
            if not rtsp_url:
                raise ValueError(f"Device {alias!r} is missing rtsp_url in config.")
            kind = str(device.get("kind") or "").strip()

            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe_alias = _safe_filename_part(alias)
            filename = f"{ts}_{safe_alias}.jpg"
            out_path = _local_cameras_dir() / filename
            timeout_s = int(arguments.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)

            _capture_rtsp_frame(rtsp_url, out_path, timeout_s)
            size_bytes = out_path.stat().st_size

            _publish_snapshot_event(
                snapshot_path=str(out_path),
                filename=filename,
                alias=device.get("alias") or alias,
                kind=kind,
                captured_at_utc=ts,
            )

            return ToolResult(
                result_type="local_camera_snapshot",
                content=f"Captured frame from {device.get('alias') or alias} ({size_bytes} bytes).",
                data={
                    "camera_alias": device.get("alias") or alias,
                    "kind": kind,
                    "snapshot_path": str(out_path),
                    "size_bytes": size_bytes,
                    "captured_at_utc": ts,
                },
            )
        except Exception as e:
            logger.error("local_camera_snapshot execution failed: %s", e)
            logger.debug("local_camera_snapshot exception details", exc_info=True)
            return make_tool_error(
                error_code="local_camera_snapshot_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
                details={"tool_name": "local_camera_snapshot"},
            )


def get_tool_class():
    return LocalCameraSnapshotTool
