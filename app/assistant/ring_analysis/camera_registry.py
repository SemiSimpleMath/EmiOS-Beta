"""Camera registry — declarative metadata for every camera Emi knows about.

Loads `configs/cameras.json` once at startup and exposes lookup by camera id
or by `match_kind` tag (used when a capture event doesn't carry a stable
camera_id, e.g. a local RTSP capture tagged kind="bedroom").

Each camera entry declares:
  - id              : stable identifier the trigger event carries
  - name / alias    : human label, optional
  - storage_folder  : repo-relative dir where JPEGs + sidecars + .meta.json land
  - analyzer        : name of the agent (registered in agent_factory) that
                      vision-analyzes each frame
  - trigger         : how frames arrive
                      type: "external_event" → we just subscribe; cadence is
                      reserved for the future when the dispatcher will own
                      scheduling too
  - post_handlers   : list of named functions in camera_post_handlers.py to
                      run after analysis (e.g. "bedroom_emergency_alarm")
  - pod_policy      : when to mint an image pod from an analyzed frame
                      conditions: list of {field, equals|min|max} predicates
                      source_kind: tag stamped onto the minted pod
                      one_liner_field / body_field: which analyzer-output
                      field to use for the pod's one_liner / body
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parent.parent.parent.parent / "configs" / "cameras.json"

_cache: Optional[List[Dict[str, Any]]] = None


def load_cameras(*, force_reload: bool = False) -> List[Dict[str, Any]]:
    """Return the camera registry, loading lazily on first call."""
    global _cache
    if _cache is not None and not force_reload:
        return _cache

    if not _REGISTRY_PATH.exists():
        raise FileNotFoundError(f"camera registry not found: {_REGISTRY_PATH}")

    raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    cameras = raw.get("cameras")
    if not isinstance(cameras, list):
        raise ValueError(f"camera registry 'cameras' must be a list (got {type(cameras).__name__})")

    for cam in cameras:
        cid = str(cam.get("id") or "").strip()
        if not cid:
            raise ValueError(f"camera entry missing required 'id': {cam}")
        if not cam.get("analyzer"):
            raise ValueError(f"camera {cid} missing required 'analyzer'")
        if not cam.get("storage_folder"):
            raise ValueError(f"camera {cid} missing required 'storage_folder'")

    _cache = cameras
    logger.info("[camera_registry] loaded %d camera(s) from %s", len(cameras), _REGISTRY_PATH.name)
    return cameras


def find_by_id(camera_id: str) -> Optional[Dict[str, Any]]:
    cid = str(camera_id or "").strip()
    if not cid:
        return None
    for cam in load_cameras():
        if str(cam.get("id") or "").strip() == cid:
            return cam
    return None


def find_by_match_kind(kind: str) -> Optional[Dict[str, Any]]:
    """Match a camera by its trigger.match_kind tag.

    Local RTSP captures (Tapo / Wyze / Reolink via local_camera_snapshot)
    don't carry the original Ring camera_id; instead they tag the event
    with `kind=bedroom` / `kind=doorbell` etc. The registry's
    `trigger.match_kind` field declares the expected tag.
    """
    k = str(kind or "").strip().lower()
    if not k:
        return None
    for cam in load_cameras():
        trigger = cam.get("trigger") or {}
        if str(trigger.get("match_kind") or "").strip().lower() == k:
            return cam
    return None


def event_topics() -> set[str]:
    """Set of distinct event_topic values across all external_event triggers.
    The dispatcher subscribes once to each topic in this set.
    """
    out: set[str] = set()
    for cam in load_cameras():
        trig = cam.get("trigger") or {}
        if str(trig.get("type") or "").strip() == "external_event":
            topic = str(trig.get("event_topic") or "").strip()
            if topic:
                out.add(topic)
    return out
