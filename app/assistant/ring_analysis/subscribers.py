"""Ring snapshot subscribers — tiny one-shot dispatchers per camera.

Wired in initialize_system. Each fresh ``ring_snapshot_captured`` event
creates a brand-new agent via the factory, calls it once, writes the
result to a ``.txt`` sidecar next to the JPEG. No persistent agent
instance, no inherited blackboard state — the agents are pure functions
of (image, prompt) → structured dict.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)


# Camera ids — pinned to today's deployment. Two cameras, two analyzers.
DOORBELL_CAMERA_ID = "74818139"     # Front Door
BEDROOM_CAMERA_ID = "158991560"     # Downstairs cam re-aimed at bed

# Comparison window for sleep_analyzer's "previous frame" lookup.
_MAX_PAIR_GAP_SECONDS = 10 * 60

# Importance score that flags a frame as an emergency. Per the
# sleep_analyzer system prompt: 10 = "Distress, fire, any emergency."
_EMERGENCY_IMPORTANCE = 10


def register_ring_subscribers() -> None:
    """Wire camera-snapshot subscribers onto event_hub. Call once at startup."""
    DI.event_hub.register_event("ring_snapshot_captured", _on_doorbell_frame)
    DI.event_hub.register_event("ring_snapshot_captured", _on_bedroom_frame)
    logger.info("Ring subscribers registered (door_bell_analyzer, sleep_analyzer).")


# ---------------------------------------------------------------------------
# Doorbell — caption + significance
# ---------------------------------------------------------------------------

def _on_doorbell_frame(message: Message) -> None:
    try:
        payload = _payload(message)
        # Match either the legacy Ring doorbell id OR any local-camera frame
        # whose `kind` field is "doorbell".
        camera_id = str(payload.get("camera_id") or "")
        kind = str(payload.get("kind") or "").strip().lower()
        if camera_id != DOORBELL_CAMERA_ID and kind != "doorbell":
            return
        jpeg, sidecar = _resolve_paths(payload)
        if jpeg is None or sidecar is None:
            return

        agent_input = {
            "date_time": get_local_time_str(),
            "image": str(jpeg),
            "camera_id": DOORBELL_CAMERA_ID,
            "captured_at_utc": str(payload.get("captured_at_utc") or ""),
        }
        agent = DI.agent_factory.create_agent("door_bell_analyzer")
        result = agent.action_handler(Message(agent_input=agent_input))
        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            logger.warning("[door_bell_analyzer] no structured data for %s", jpeg.name)
            return
        caption = str(data.get("caption") or "").strip()
        if not caption:
            logger.warning("[door_bell_analyzer] LLM returned no caption for %s", jpeg.name)
            return

        is_significant = bool(data.get("is_significant"))
        significance_reason = str(data.get("significance_reason") or "").strip()
        sidecar.write_text(
            _doorbell_sidecar_text(caption, is_significant, significance_reason, agent_input),
            encoding="utf-8",
        )
        logger.info(
            "[door_bell_analyzer] captioned %s (significant=%s)",
            jpeg.name, is_significant,
        )
        if is_significant:
            # TODO: mint a pod from this snapshot — kind=image, body=caption.
            logger.info(
                "[door_bell_analyzer] SIGNIFICANT (pod creation deferred): %s — %s",
                jpeg.name, significance_reason or "(no reason given)",
            )
    except Exception as e:
        logger.error("[door_bell_analyzer] subscriber crashed: %s", e, exc_info=True)


def _doorbell_sidecar_text(
    caption: str, is_significant: bool, significance_reason: str, ai: Dict[str, Any],
) -> str:
    lines = [
        caption,
        "",
        f"camera_id: {ai.get('camera_id', '')}",
        f"captured_at_utc: {ai.get('captured_at_utc', '')}",
        f"analyzed_at_local: {ai.get('date_time', '')}",
        f"analyzer: door_bell_analyzer",
        f"is_significant: {str(is_significant).lower()}",
    ]
    if significance_reason:
        lines.append(f"significance_reason: {significance_reason}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Bedroom — two-frame compare + importance score
# ---------------------------------------------------------------------------

def _on_bedroom_frame(message: Message) -> None:
    try:
        payload = _payload(message)
        # Match either:
        #   (a) the legacy hardcoded Ring bedroom camera id, OR
        #   (b) any local-camera snapshot whose `kind` field is "bedroom"
        #       (Tapo / Wyze / Reolink etc. configured in
        #        smart_home_tools.json[local_cameras])
        camera_id = str(payload.get("camera_id") or "")
        kind = str(payload.get("kind") or "").strip().lower()
        if camera_id != BEDROOM_CAMERA_ID and kind != "bedroom":
            return
        jpeg, sidecar = _resolve_paths(payload)
        if jpeg is None or sidecar is None:
            return

        previous = _find_previous_bedroom_frame(jpeg)
        previous_label = previous.name if previous else "(none — first frame in window)"

        agent_input = {
            "date_time": get_local_time_str(),
            "image": str(jpeg),
            "previous_image": str(previous) if previous else "",
            "previous_image_label": previous_label,
            "has_previous": bool(previous),
            "camera_id": BEDROOM_CAMERA_ID,
            "captured_at_utc": str(payload.get("captured_at_utc") or ""),
        }
        agent = DI.agent_factory.create_agent("sleep_analyzer")
        result = agent.action_handler(Message(agent_input=agent_input))
        data = getattr(result, "data", None)
        if not isinstance(data, dict):
            logger.warning("[sleep_analyzer] no structured data for %s", jpeg.name)
            return

        sidecar.write_text(_sleep_sidecar_text(data, agent_input), encoding="utf-8")
        logger.info(
            "[sleep_analyzer] analyzed %s — in_bed=%s pos=%s motion=%s importance=%s",
            jpeg.name,
            data.get("subject_in_bed"),
            data.get("position"),
            data.get("motion_vs_previous"),
            data.get("importance"),
        )

        # int(float(...)) handles both "10" and "10.0" / 10.0; bare int()
        # raises ValueError on "10.0" and would silently drop a real
        # emergency.
        raw_imp = data.get("importance")
        try:
            importance_int = int(float(raw_imp)) if raw_imp not in (None, "") else 0
        except (TypeError, ValueError):
            importance_int = 0
        if importance_int >= _EMERGENCY_IMPORTANCE:
            _trigger_bedroom_emergency_alarm(
                jpeg=jpeg, sidecar=sidecar,
                data=data, agent_input=agent_input,
                importance=importance_int,
            )
    except Exception as e:
        logger.error("[sleep_analyzer] subscriber crashed: %s", e, exc_info=True)


def _trigger_bedroom_emergency_alarm(
    *,
    jpeg: Path,
    sidecar: Path,
    data: Dict[str, Any],
    agent_input: Dict[str, Any],
    importance: int,
) -> None:
    """Sleep analyzer flagged an importance-10 frame (distress / fire /
    emergency per its scale). Sound an alarm.

    Three actions, all best-effort and independent — any one failing
    must NOT swallow the others:

    1. CRITICAL log line — visible in tail/grep, even with no other
       infrastructure.
    2. Companion `.EMERGENCY.txt` sidecar — a frame-adjacent marker
       that stands out among normal sleep sidecars when grepping the
       snapshot directory.
    3. ``bedroom_emergency_detected`` event_hub publish — anything else
       (SMS, smart-home siren, push notification) subscribes here. Keeps
       the subscriber-vs-actuator separation clean.
    """
    description = str(data.get("description") or "").strip() or "(no description)"
    importance_reason = str(data.get("importance_reason") or "").strip()
    notes = str(data.get("notes") or "").strip()

    logger.critical(
        "[BEDROOM-EMERGENCY] [sleep_analyzer] importance=%s frame=%s "
        "reason=%r description=%r",
        importance, jpeg.name, importance_reason, description,
    )

    try:
        emergency_path = jpeg.with_name(jpeg.stem + ".EMERGENCY.txt")
        emergency_lines = [
            "🚨 BEDROOM EMERGENCY 🚨",
            "",
            f"importance: {importance}",
            f"reason: {importance_reason or '(not given)'}",
            f"description: {description}",
        ]
        if notes:
            emergency_lines.append(f"notes: {notes}")
        emergency_lines += [
            "",
            f"frame: {jpeg.name}",
            f"sidecar: {sidecar.name}",
            f"camera_id: {agent_input.get('camera_id', '')}",
            f"captured_at_utc: {agent_input.get('captured_at_utc', '')}",
            f"detected_at_local: {agent_input.get('date_time', '')}",
        ]
        emergency_path.write_text("\n".join(emergency_lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.error(
            "[sleep_analyzer] failed to write EMERGENCY sidecar for %s: %s",
            jpeg.name, e, exc_info=True,
        )

    try:
        DI.event_hub.publish(
            Message(
                data_type="event",
                sender="sleep_analyzer",
                receiver=None,
                event_topic="bedroom_emergency_detected",
                content=description,
                data={
                    "importance": importance,
                    "importance_reason": importance_reason,
                    "description": description,
                    "notes": notes,
                    "frame": str(jpeg),
                    "sidecar": str(sidecar),
                    "camera_id": agent_input.get("camera_id"),
                    "captured_at_utc": agent_input.get("captured_at_utc"),
                    "detected_at_local": agent_input.get("date_time"),
                    "subject_in_bed": data.get("subject_in_bed"),
                    "position": data.get("position"),
                    "light_state": data.get("light_state"),
                    "cpap_state": data.get("cpap_state"),
                },
            )
        )
    except Exception as e:
        logger.error(
            "[sleep_analyzer] failed to publish bedroom_emergency_detected event: %s",
            e, exc_info=True,
        )


def _find_previous_bedroom_frame(current: Path) -> Optional[Path]:
    """Return the most recent prior frame from the SAME camera within the
    comparison window, or None if the current frame is the first one in
    the window.

    Camera identity is derived from the current filename's `<ts>_<camera>.jpg`
    suffix — works whether <camera> is the legacy Ring camera_id or a
    local-camera alias like 'Emi1'. Previously hardcoded BEDROOM_CAMERA_ID
    here, which broke pairing for any non-Ring camera.
    """
    try:
        snap_dir = current.parent
        current_mtime = current.stat().st_mtime
        # Parse the current file's camera identifier from its name.
        stem = current.stem
        if "_" not in stem:
            return None
        _, _, cam_part = stem.partition("_")
        if not cam_part:
            return None
        cam_suffix = f"_{cam_part}.jpg"
        best: Optional[Path] = None
        best_mtime = -1.0
        for p in snap_dir.iterdir():
            if not p.is_file() or p == current or not p.name.endswith(cam_suffix):
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
        logger.warning("[sleep_analyzer] previous-frame lookup failed: %s", e)
        return None


def _sleep_sidecar_text(data: Dict[str, Any], ai: Dict[str, Any]) -> str:
    description = str(data.get("description") or "").strip()
    notes = str(data.get("notes") or "").strip()
    awake = data.get("awake_indicators") or []
    awake_str = (
        "; ".join(str(x).strip() for x in awake if str(x).strip())
        if isinstance(awake, list) else str(awake).strip()
    )
    try:
        importance = int(data.get("importance"))
    except (TypeError, ValueError):
        importance = 0
    importance = max(0, min(10, importance))
    importance_reason = str(data.get("importance_reason") or "").strip()

    lines = [
        description if description else "(no description)",
    ]
    if notes:
        lines.append("")
        lines.append(f"notes: {notes}")
    lines += [
        "",
        f"camera_id: {ai.get('camera_id', '')}",
        f"captured_at_utc: {ai.get('captured_at_utc', '')}",
        f"analyzed_at_local: {ai.get('date_time', '')}",
        f"analyzer: sleep_analyzer",
        f"previous_image: {ai.get('previous_image_label', '')}",
        f"subject_in_bed: {str(bool(data.get('subject_in_bed'))).lower()}",
        f"position: {str(data.get('position') or 'unclear').strip()}",
        f"motion_vs_previous: {str(data.get('motion_vs_previous') or 'unclear').strip()}",
        f"light_state: {str(data.get('light_state') or 'unclear').strip()}",
        f"cpap_state: {str(data.get('cpap_state') or 'unclear').strip()}",
        f"awake_indicators: {awake_str}",
        f"importance: {importance}",
    ]
    if importance_reason:
        lines.append(f"importance_reason: {importance_reason}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _payload(message: Message) -> Dict[str, Any]:
    for attr in ("data", "tool_data", "metadata"):
        v = getattr(message, attr, None)
        if isinstance(v, dict) and v:
            return v
    ai = getattr(message, "agent_input", None)
    return ai if isinstance(ai, dict) else {}


def _resolve_paths(payload: Dict[str, Any]) -> tuple[Optional[Path], Optional[Path]]:
    """Return (jpeg_path, sidecar_path). Returns (None, None) if the JPEG is
    missing or a sidecar already exists (idempotent skip).
    """
    snapshot_path = str(payload.get("snapshot_path") or "").strip()
    if not snapshot_path:
        logger.warning("ring_snapshot_captured event missing snapshot_path; skipping")
        return None, None
    jpeg = Path(snapshot_path)
    if not jpeg.exists():
        logger.warning("snapshot vanished before analysis: %s", jpeg)
        return None, None
    sidecar = jpeg.with_suffix(".txt")
    if sidecar.exists():
        logger.info("sidecar already exists for %s; skipping", jpeg.name)
        return None, None
    return jpeg, sidecar


