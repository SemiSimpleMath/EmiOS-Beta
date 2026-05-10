"""Routine handler that polls one Ring camera for new motion / ding events.

Each Ring camera gets its own routine entry in `configs/routines.json`,
naming this handler with `spec.camera_id` set to the Ring device id. The
handler fetches Ring's recent-events history for that one camera,
compares against the per-camera watermark in
`data/ring_motion_watermark.json`, and if any new events are present
captures a single fresh snapshot via the existing Ring bridge — which
writes the JPEG to disk and publishes `ring_snapshot_captured`. The
`camera_dispatch` routine then runs the per-camera analyzer (declared
in `configs/cameras.json`), writes sidecars, and mints the image pod.

One snapshot per poll regardless of event count: a single fresh frame
answers "what's happening at the camera right now?" — capturing one per
event would multiply Ring API work without giving the analyzer any new
information.

Per-camera routine entries let each camera have its own cadence,
active_window, watchdog budget, and on_error policy. Front Door (always
active) and Downstairs (active 00:00-06:00) are independent routines
that share this handler with different `spec.camera_id`.
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


@routine_handler(name="ring_camera_motion_poll")
def ring_camera_motion_poll(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    spec = (routine.spec if routine and isinstance(getattr(routine, "spec", None), dict) else {}) or {}
    camera_id = str(spec.get("camera_id") or "").strip()
    if not camera_id:
        logger.error("ring_camera_motion_poll: spec.camera_id is required")
        return {"status": "error", "error": "missing camera_id in routine spec"}

    if not _camera_id_is_ring(camera_id):
        logger.error("ring_camera_motion_poll: camera_id %s is not a registered Ring camera", camera_id)
        return {"status": "error", "error": f"camera_id {camera_id!r} not found as a Ring camera in configs/cameras.json"}

    lookback_minutes = int(spec.get("lookback_minutes") or _DEFAULT_LOOKBACK_MINUTES)

    watermark_path = get_repo_root() / "data" / _WATERMARK_FILE
    watermark = _load_watermark(watermark_path)
    previous_wm = watermark.get(camera_id)
    cold_start = not previous_wm

    try:
        history = _fetch_recent_events(camera_id, lookback_minutes)
    except Exception as e:
        logger.warning("[ring_camera_motion_poll] fetch_recent_events failed for %s: %s", camera_id, e)
        return {"status": "error", "error": str(e), "camera_id": camera_id}

    events = history.get("events") or []
    new_events = _select_new_events(events, previous_wm)

    captures = 0
    if new_events:
        logger.info(
            "[ring_camera_motion_poll] camera=%s saw %d new event(s); attempting capture",
            camera_id, len(new_events),
        )
        try:
            _capture_snapshot(camera_id)
            captures = 1
            logger.info("[ring_camera_motion_poll] camera=%s capture succeeded", camera_id)
        except Exception as e:
            logger.warning(
                "[ring_camera_motion_poll] camera=%s snapshot capture FAILED (%d new events): %s",
                camera_id, len(new_events), e,
                exc_info=True,
            )

    new_wm = _max_event_timestamp(events) or (
        datetime.now(timezone.utc).isoformat() if cold_start else previous_wm
    )
    if new_wm and new_wm != previous_wm:
        watermark[camera_id] = new_wm
        _save_watermark(watermark_path, watermark)

    return {
        "status": "ok",
        "camera_id": camera_id,
        "events_seen": len(events),
        "new_events": len(new_events),
        "captures": captures,
        "cold_start": cold_start,
    }


def _camera_id_is_ring(camera_id: str) -> bool:
    """True iff camera_id is in configs/cameras.json with the Ring topic + numeric id shape."""
    from app.assistant.ring_analysis import camera_registry
    for cam in camera_registry.load_cameras():
        if str(cam.get("id") or "").strip() != camera_id:
            continue
        trig = cam.get("trigger") or {}
        if trig.get("event_topic") != "ring_snapshot_captured":
            return False
        return camera_id.isdigit()
    return False


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
        logger.warning("[ring_camera_motion_poll] watermark read failed (%s); starting fresh", e)
        return {}


def _save_watermark(path: Path, data: Dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("[ring_camera_motion_poll] watermark save failed: %s", e)


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
