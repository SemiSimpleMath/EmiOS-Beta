"""Single subscriber that handles every camera-snapshot event.

Replaces the per-camera hardcoded subscribers in the old subscribers.py.
Reads the camera registry, looks up the camera by id (or match_kind for
local RTSP frames), and runs the full post-capture pipeline:

  1. Move/copy the JPEG into the camera's per-camera folder
  2. Run the configured analyzer agent on the frame
  3. Write a human-readable .txt sidecar next to the JPEG
  4. Write a structured .meta.json sidecar next to the JPEG
  5. Evaluate pod_policy → mint an image pod (or skip)
  6. Run any configured post_handlers (e.g. bedroom_emergency_alarm)

The dispatcher is registered once per distinct event topic across the
registry. Each frame is routed by camera_id with a fallback to
trigger.match_kind for local RTSP captures.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.ring_analysis import camera_registry
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)

# Window for "previous frame" lookup used by sleep_analyzer-like agents.
_MAX_PAIR_GAP_SECONDS = 10 * 60


# ---------------------------------------------------------------------------
# Public entry point — register one dispatcher per distinct event topic.
# ---------------------------------------------------------------------------

def register_camera_dispatcher() -> None:
    """Wire the dispatcher onto event_hub for each topic the registry uses."""
    for topic in camera_registry.event_topics():
        DI.event_hub.register_event(topic, _on_camera_event)
        logger.info("[camera_dispatcher] subscribed to event topic %r", topic)
    cams = camera_registry.load_cameras()
    logger.info(
        "[camera_dispatcher] registered (%d camera(s): %s)",
        len(cams), ", ".join(c.get("name", c.get("id", "?")) for c in cams),
    )


# ---------------------------------------------------------------------------
# Core dispatch
# ---------------------------------------------------------------------------

def _on_camera_event(message: Message) -> None:
    try:
        payload = _payload(message)
        camera = _resolve_camera(payload)
        if camera is None:
            # Not an error — events for cameras we don't know about are
            # ignored silently. Other subscribers may still want them.
            return

        src_jpeg = _extract_source_jpeg(payload)
        if src_jpeg is None:
            return

        captured_at_utc = str(payload.get("captured_at_utc") or "").strip()
        target_jpeg = _relocate_to_camera_folder(camera, src_jpeg, captured_at_utc)
        sidecar = target_jpeg.with_suffix(".txt")
        meta_json = target_jpeg.with_suffix(".meta.json")
        if sidecar.exists():
            logger.debug("[camera_dispatcher] sidecar already exists for %s; skipping", target_jpeg.name)
            return

        # 2. Run the configured analyzer.
        analyzer_name = str(camera.get("analyzer") or "").strip()
        if not analyzer_name:
            logger.warning("[camera_dispatcher] camera %s has no analyzer configured", camera.get("id"))
            return

        agent_input = _build_agent_input(camera, target_jpeg, captured_at_utc)
        agent = DI.agent_factory.create_agent(analyzer_name)
        if agent is None:
            logger.error("[camera_dispatcher] failed to create analyzer agent: %s", analyzer_name)
            return
        result = agent.action_handler(Message(agent_input=agent_input))
        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            logger.warning("[%s] no structured data for %s", analyzer_name, target_jpeg.name)
            return

        # 3 + 4. Write sidecar files.
        sidecar.write_text(_format_text_sidecar(camera, data, agent_input), encoding="utf-8")
        meta_json.write_text(json.dumps({
            "camera_id": camera.get("id"),
            "camera_name": camera.get("name"),
            "analyzer": analyzer_name,
            "captured_at_utc": captured_at_utc,
            "analyzed_at_local": agent_input.get("date_time"),
            "result": data,
        }, indent=2, default=str), encoding="utf-8")

        # 5. Evaluate pod policy.
        pod_policy = camera.get("pod_policy") or {}
        if _pod_policy_matches(pod_policy, data):
            _mint_camera_pod(camera, target_jpeg, data, captured_at_utc, analyzer_name)

        # 6. Run any post-handlers (named functions in camera_post_handlers.py).
        for handler_name in (camera.get("post_handlers") or []):
            _run_post_handler(handler_name, camera=camera, jpeg=target_jpeg, sidecar=sidecar,
                              data=data, agent_input=agent_input)

        logger.info(
            "[%s] analyzed %s → pod=%s",
            analyzer_name, target_jpeg.name,
            "minted" if _pod_policy_matches(pod_policy, data) else "skipped",
        )
    except Exception as e:
        logger.error("[camera_dispatcher] crash: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Camera resolution
# ---------------------------------------------------------------------------

def _resolve_camera(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cid = str(payload.get("camera_id") or "").strip()
    if cid:
        cam = camera_registry.find_by_id(cid)
        if cam is not None:
            return cam
    kind = str(payload.get("kind") or "").strip().lower()
    if kind:
        return camera_registry.find_by_match_kind(kind)
    return None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _relocate_to_camera_folder(
    camera: Dict[str, Any], src: Path, captured_at_utc: str,
) -> Path:
    """Move the JPEG into the camera's per-camera folder, partitioned by date.

    Idempotent: if the target already exists, the source is left in place
    and the existing target is returned.
    """
    folder_str = str(camera.get("storage_folder") or "").strip()
    if not folder_str:
        return src

    base = Path(folder_str)
    if not base.is_absolute():
        base = get_repo_root() / base

    # Date-partition by capture time (falls back to current local date).
    date_str = _date_subfolder_for(captured_at_utc)
    target_dir = base / date_str
    target_dir.mkdir(parents=True, exist_ok=True)

    target = target_dir / src.name
    if target.exists():
        return target

    try:
        shutil.move(str(src), str(target))
    except Exception as e:
        # If move fails (cross-device, locked file), fall back to copy.
        logger.warning("[camera_dispatcher] move failed (%s); copying instead", e)
        shutil.copy2(str(src), str(target))

    return target


def _date_subfolder_for(captured_at_utc: str) -> str:
    if captured_at_utc:
        try:
            ts = datetime.fromisoformat(captured_at_utc.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Analyzer input
# ---------------------------------------------------------------------------

def _build_agent_input(
    camera: Dict[str, Any], jpeg: Path, captured_at_utc: str,
) -> Dict[str, Any]:
    agent_input: Dict[str, Any] = {
        "date_time": get_local_time_str(),
        "image": str(jpeg),
        "camera_id": camera.get("id", ""),
        "captured_at_utc": captured_at_utc,
    }

    # Sleep-style analyzers want the previous frame for two-frame compare.
    # We surface it for every analyzer; analyzers that don't reference it
    # in their prompt simply ignore it.
    previous = _find_previous_frame(camera, jpeg)
    agent_input["previous_image"] = str(previous) if previous else ""
    agent_input["previous_image_label"] = (
        previous.name if previous else "(none — first frame in window)"
    )
    agent_input["has_previous"] = bool(previous)
    return agent_input


def _find_previous_frame(camera: Dict[str, Any], current: Path) -> Optional[Path]:
    """Most recent frame from the same camera within the comparison window.

    With per-camera folders, this is just "newest .jpg older than current
    in the same folder (or in the previous date-partition folder, if we're
    near a midnight boundary)."
    """
    try:
        current_mtime = current.stat().st_mtime
        candidate_dirs: List[Path] = [current.parent]
        # Also peek at the previous date-partition folder so we don't miss
        # the previous frame across a midnight rollover.
        prev_day_dir = current.parent.parent / _previous_day(current.parent.name)
        if prev_day_dir.is_dir():
            candidate_dirs.append(prev_day_dir)

        best: Optional[Path] = None
        best_mtime = -1.0
        for d in candidate_dirs:
            for p in d.iterdir():
                if not p.is_file() or p == current or p.suffix.lower() != ".jpg":
                    continue
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if m >= current_mtime:
                    continue
                if m > best_mtime:
                    best_mtime = m
                    best = p
        if best is None or (current_mtime - best_mtime) > _MAX_PAIR_GAP_SECONDS:
            return None
        return best
    except Exception as e:
        logger.warning("[camera_dispatcher] previous-frame lookup failed: %s", e)
        return None


def _previous_day(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        from datetime import timedelta
        return (d - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Pod policy
# ---------------------------------------------------------------------------

def _pod_policy_matches(policy: Dict[str, Any], data: Dict[str, Any]) -> bool:
    if not policy:
        return False
    conditions = policy.get("conditions") or []
    if not conditions:
        return False
    for cond in conditions:
        field = cond.get("field")
        if not field:
            return False
        actual = data.get(field)
        if "equals" in cond:
            if actual != cond["equals"]:
                return False
        if "min" in cond:
            try:
                if float(actual) < float(cond["min"]):
                    return False
            except (TypeError, ValueError):
                return False
        if "max" in cond:
            try:
                if float(actual) > float(cond["max"]):
                    return False
            except (TypeError, ValueError):
                return False
    return True


def _mint_camera_pod(
    camera: Dict[str, Any], jpeg: Path, data: Dict[str, Any],
    captured_at_utc: str, analyzer: str,
) -> None:
    try:
        from app.assistant.pod_store.image_ingest import ingest_image_file
        from app.assistant.pod_store.pod_store import PodStore

        policy = camera.get("pod_policy") or {}
        source_kind = str(policy.get("source_kind") or f"camera_{camera.get('id', 'unknown')}")
        body_field = str(policy.get("body_field") or "")
        one_liner_field = str(policy.get("one_liner_field") or body_field)
        body = str(data.get(body_field) or "").strip() if body_field else ""
        one_liner = str(data.get(one_liner_field) or "").strip() if one_liner_field else body

        occurred_at = None
        if captured_at_utc:
            try:
                occurred_at = datetime.fromisoformat(captured_at_utc.replace("Z", "+00:00"))
                if occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        meta_extra: Dict[str, Any] = {
            "camera_id": camera.get("id"),
            "camera_name": camera.get("name"),
            "captured_at_utc": captured_at_utc,
            "analyzer": analyzer,
        }
        # Stamp every analyzer-output field into pod metadata so consumers
        # can filter without re-reading the .meta.json sidecar.
        for k, v in (data or {}).items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                meta_extra[k] = v

        store = PodStore()
        pod = ingest_image_file(
            src_path=jpeg,
            source_kind=source_kind,
            pod_store=store,
            metadata_extra=meta_extra,
            occurred_at_utc=occurred_at,
        )

        # Backfill body/one_liner from the analyzer's output if the pod is
        # newly minted (vision_extraction_status=pending).
        if not (pod.body or "").strip() and (body or one_liner):
            pod.body = body or one_liner
            pod.one_liner = (one_liner or body)[:140]
            if isinstance(pod.metadata, dict):
                pod.metadata["vision_extraction_status"] = "done"
            store.put(pod)

        logger.info(
            "[%s] minted pod %s source_kind=%s",
            analyzer, pod.pod_id, source_kind,
        )
    except Exception as e:
        logger.error(
            "[camera_dispatcher] pod minting failed for %s: %s",
            jpeg.name, e, exc_info=True,
        )


# ---------------------------------------------------------------------------
# Post-handlers (registry-driven)
# ---------------------------------------------------------------------------

def _run_post_handler(name: str, **kwargs: Any) -> None:
    try:
        from app.assistant.ring_analysis import camera_post_handlers
        fn = getattr(camera_post_handlers, name, None)
        if fn is None:
            logger.warning("[camera_dispatcher] unknown post_handler: %s", name)
            return
        fn(**kwargs)
    except Exception as e:
        logger.error("[camera_dispatcher] post_handler %s crashed: %s", name, e, exc_info=True)


# ---------------------------------------------------------------------------
# Sidecar formatting
# ---------------------------------------------------------------------------

def _format_text_sidecar(
    camera: Dict[str, Any], data: Dict[str, Any], ai: Dict[str, Any],
) -> str:
    """Build a human-readable .txt sidecar.

    Generic shape: leads with description / caption (the analyzer's prose
    output), then a machine-friendly key:value tail. Specific analyzers
    can rely on this consistent shape, or read .meta.json if they need
    structured fields.
    """
    description = (
        str(data.get("description") or "").strip()
        or str(data.get("caption") or "").strip()
        or "(no description)"
    )
    notes = str(data.get("notes") or "").strip()

    lines: List[str] = [description]
    if notes:
        lines += ["", f"notes: {notes}"]

    lines += [
        "",
        f"camera_id: {camera.get('id', '')}",
        f"camera_name: {camera.get('name', '')}",
        f"captured_at_utc: {ai.get('captured_at_utc', '')}",
        f"analyzed_at_local: {ai.get('date_time', '')}",
        f"analyzer: {camera.get('analyzer', '')}",
    ]
    # Analyzer-specific fields appended in stable order.
    skip = {"description", "caption", "notes"}
    for key in sorted(data.keys()):
        if key in skip:
            continue
        value = data[key]
        if isinstance(value, (list, dict)):
            value = json.dumps(value, default=str)
        lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _payload(message: Message) -> Dict[str, Any]:
    for attr in ("data", "tool_data", "metadata"):
        v = getattr(message, attr, None)
        if isinstance(v, dict) and v:
            return v
    ai = getattr(message, "agent_input", None)
    return ai if isinstance(ai, dict) else {}


def _extract_source_jpeg(payload: Dict[str, Any]) -> Optional[Path]:
    snapshot_path = str(payload.get("snapshot_path") or "").strip()
    if not snapshot_path:
        logger.warning("snapshot event missing snapshot_path; skipping")
        return None
    p = Path(snapshot_path)
    if not p.exists():
        logger.warning("snapshot vanished before analysis: %s", p)
        return None
    return p
