"""Routine handler that polls Ring for new motion / ding events.

Walks every camera registered in `configs/cameras.json` whose
`trigger.event_topic == "ring_snapshot_captured"` and whose id is a Ring
device id. For each, fetches Ring's recent-events history (motion + ding
+ on-demand events). Compares against a per-camera watermark stored at
`data/ring_motion_watermark.json`. For each event newer than the
watermark the handler captures a snapshot via the existing Ring bridge —
which writes the JPEG to disk and publishes `ring_snapshot_captured`.
The `camera_dispatch` routine then runs the analyzer, writes sidecars,
and mints the image pod (existing chain).

Net effect: motion at the front door (or in the bedroom) → fresh frame
captured + analyzed without any user action.

Wired in `configs/routines.json` under id `ring_motion_poll`.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_WATERMARK_FILE = "ring_motion_watermark.json"
_DEFAULT_LOOKBACK_MINUTES = 5


@routine_handler(name="ring_motion_poll")
def ring_motion_poll(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    spec = (routine.spec if routine and isinstance(getattr(routine, "spec", None), dict) else {}) or {}
    lookback_minutes = int(spec.get("lookback_minutes") or _DEFAULT_LOOKBACK_MINUTES)

    cameras = _ring_cameras_from_registry()
    if not cameras:
        return {"status": "ok", "polled": 0, "captures": 0, "note": "no Ring cameras configured"}

    watermark_path = get_repo_root() / "data" / _WATERMARK_FILE
    watermark = _load_watermark(watermark_path)

    total_captures = 0
    total_new_events = 0
    watermark_changed = False
    per_camera: List[Dict[str, Any]] = []

    for cam in cameras:
        cam_id = str(cam.get("id") or "").strip()
        if not cam_id:
            continue
        try:
            history = _fetch_recent_events(cam_id, lookback_minutes)
        except Exception as e:
            logger.warning("[ring_motion_poll] fetch_recent_events failed for %s: %s", cam_id, e)
            per_camera.append({"camera_id": cam_id, "name": cam.get("name"), "error": str(e)})
            continue

        events = history.get("events") or []
        previous_wm = watermark.get(cam_id)
        cold_start = not previous_wm
        new_events = _select_new_events(events, previous_wm)
        captures = 0
        for ev in new_events:
            try:
                _capture_snapshot(cam_id)
                captures += 1
            except Exception as e:
                logger.warning(
                    "[ring_motion_poll] snapshot capture failed for %s on event %s: %s",
                    cam_id, ev.get("id"), e,
                )
                break
        # Update watermark in three cases:
        #   1. cold start with any events seen — pin to newest so we don't
        #      replay history next tick
        #   2. cold start with zero events — pin to "now" so we don't
        #      keep cold-starting forever
        #   3. new events captured — advance to the newest seen
        new_wm = _max_event_timestamp(events) or (
            datetime.now(timezone.utc).isoformat() if cold_start else previous_wm
        )
        if new_wm and new_wm != previous_wm:
            watermark[cam_id] = new_wm
            watermark_changed = True
        total_new_events += len(new_events)
        total_captures += captures
        per_camera.append({
            "camera_id": cam_id,
            "name": cam.get("name"),
            "events_seen": len(events),
            "new_events": len(new_events),
            "captures": captures,
            "cold_start": cold_start,
        })

    if watermark_changed:
        _save_watermark(watermark_path, watermark)

    return {
        "status": "ok",
        "polled": len(cameras),
        "new_events": total_new_events,
        "captures": total_captures,
        "per_camera": per_camera,
    }


def _ring_cameras_from_registry() -> List[Dict[str, Any]]:
    """Return camera registry entries that look like Ring (by event topic + numeric id)."""
    from app.assistant.ring_analysis import camera_registry
    out: List[Dict[str, Any]] = []
    for cam in camera_registry.load_cameras():
        trig = cam.get("trigger") or {}
        if trig.get("event_topic") != "ring_snapshot_captured":
            continue
        cam_id = str(cam.get("id") or "").strip()
        if not cam_id or not cam_id.isdigit():
            continue
        out.append(cam)
    return out


def _fetch_recent_events(camera_id: str, lookback_minutes: int) -> Dict[str, Any]:
    from app.routes import smart_home_bridge as bridge
    return bridge._run_async(bridge._ring_get_recent_events(camera_id, lookback_minutes))


def _capture_snapshot(camera_id: str) -> Dict[str, Any]:
    """Trigger the bridge's get_snapshot — same code path the doorbell tool uses.

    The bridge writes the JPEG to data/ring_snapshots/ and publishes
    `ring_snapshot_captured`, which fires the camera_dispatch routine
    (analyzer + sidecars + pod mint).
    """
    from app.routes import smart_home_bridge as bridge
    return bridge._ring_dispatch("get_snapshot", {"camera_id": camera_id})


def _load_watermark(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("[ring_motion_poll] watermark read failed (%s); starting fresh", e)
        return {}


def _save_watermark(path: Path, data: Dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("[ring_motion_poll] watermark save failed: %s", e)


def _select_new_events(events: List[Dict[str, Any]], last_seen: Optional[str]) -> List[Dict[str, Any]]:
    """Return events strictly newer than the per-camera watermark.

    First poll for a camera (no watermark) does NOT replay history — it
    only sets the watermark and returns []. Otherwise an enabled
    poll-on-cold-start would burn snapshots on every old event in the
    lookback window."""
    if not events:
        return []
    if not last_seen:
        return []
    cutoff = _parse_ts(last_seen)
    if cutoff is None:
        return []
    out: List[Dict[str, Any]] = []
    for ev in events:
        ts = _parse_ts(ev.get("created_at"))
        if ts is None:
            continue
        if ts > cutoff:
            out.append(ev)
    return out


def _max_event_timestamp(events: List[Dict[str, Any]]) -> Optional[str]:
    best: Optional[datetime] = None
    for ev in events:
        ts = _parse_ts(ev.get("created_at"))
        if ts is None:
            continue
        if best is None or ts > best:
            best = ts
    return best.isoformat() if best else None


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return None
