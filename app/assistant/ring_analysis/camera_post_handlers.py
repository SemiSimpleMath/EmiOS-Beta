"""Named post-analysis handlers, referenced by camera registry entries.

Each function takes the standard kwargs the dispatcher passes:

    camera        : the camera registry entry (dict)
    jpeg          : Path to the captured frame (now in per-camera storage)
    sidecar       : Path to the .txt sidecar already written
    data          : analyzer's structured output (dict)
    agent_input   : what was fed to the analyzer agent (dict)

Failures inside a handler must NOT raise — log and continue. The
dispatcher only catches exceptions per-handler, but pod minting and
sidecar writing have already happened by the time a handler runs;
the handler's job is downstream side-effects only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

# importance threshold that flags a frame as a real emergency (per
# sleep_analyzer's prompt: 10 = "Distress, fire, any emergency.")
_EMERGENCY_IMPORTANCE = 10


def bedroom_emergency_alarm(
    *,
    camera: Dict[str, Any],
    jpeg: Path,
    sidecar: Path,
    data: Dict[str, Any],
    agent_input: Dict[str, Any],
) -> None:
    """If sleep_analyzer flagged importance>=10, sound an alarm.

    Three best-effort actions:
      1. CRITICAL log line (visible in tail/grep with no other infra).
      2. Companion `.EMERGENCY.txt` sidecar (stands out among normal sidecars).
      3. Publish ``bedroom_emergency_detected`` event_hub event for any
         downstream subscriber (SMS, smart-home siren, push notification).
    """
    raw_imp = data.get("importance")
    try:
        importance = int(float(raw_imp)) if raw_imp not in (None, "") else 0
    except (TypeError, ValueError):
        importance = 0

    if importance < _EMERGENCY_IMPORTANCE:
        return

    description = str(data.get("description") or "").strip() or "(no description)"
    importance_reason = str(data.get("importance_reason") or "").strip()
    notes = str(data.get("notes") or "").strip()

    logger.critical(
        "[BEDROOM-EMERGENCY] importance=%s frame=%s reason=%r description=%r",
        importance, jpeg.name, importance_reason, description,
    )

    try:
        emergency_path = jpeg.with_name(jpeg.stem + ".EMERGENCY.txt")
        lines = [
            "BEDROOM EMERGENCY",
            "",
            f"importance: {importance}",
            f"reason: {importance_reason or '(not given)'}",
            f"description: {description}",
        ]
        if notes:
            lines.append(f"notes: {notes}")
        lines += [
            "",
            f"frame: {jpeg.name}",
            f"sidecar: {sidecar.name}",
            f"camera_id: {camera.get('id', '')}",
            f"camera_name: {camera.get('name', '')}",
            f"captured_at_utc: {agent_input.get('captured_at_utc', '')}",
            f"detected_at_local: {agent_input.get('date_time', '')}",
        ]
        emergency_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        logger.error(
            "[bedroom_emergency_alarm] failed to write EMERGENCY sidecar for %s: %s",
            jpeg.name, e, exc_info=True,
        )

    try:
        DI.event_hub.publish(
            Message(
                data_type="event",
                sender="bedroom_emergency_alarm",
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
                    "camera_id": camera.get("id"),
                    "camera_name": camera.get("name"),
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
            "[bedroom_emergency_alarm] failed to publish event: %s", e, exc_info=True,
        )
