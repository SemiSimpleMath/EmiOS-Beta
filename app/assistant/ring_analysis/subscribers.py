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
        if str(payload.get("camera_id") or "") != DOORBELL_CAMERA_ID:
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
        data = _run_agent("door_bell_analyzer", agent_input)
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
        if str(payload.get("camera_id") or "") != BEDROOM_CAMERA_ID:
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
        data = _run_agent("sleep_analyzer", agent_input)
        if not data:
            logger.warning("[sleep_analyzer] LLM returned no analysis for %s", jpeg.name)
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
    except Exception as e:
        logger.error("[sleep_analyzer] subscriber crashed: %s", e, exc_info=True)


def _find_previous_bedroom_frame(current: Path) -> Optional[Path]:
    """Return the most recent prior bedroom JPEG within the comparison window,
    or None if the current frame is the first one in the window.
    """
    try:
        snap_dir = current.parent
        current_mtime = current.stat().st_mtime
        cam_suffix = f"_{BEDROOM_CAMERA_ID}.jpg"
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
        notes if notes else "(no notes)",
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


def _run_agent(agent_name: str, agent_input: Dict[str, Any]) -> Dict[str, Any]:
    """Create a fresh agent and run it once. Returns the LLM result as a dict."""
    agent = DI.agent_factory.create_agent(agent_name)
    if agent is None:
        raise RuntimeError(f"agent_factory.create_agent returned None for {agent_name!r}")
    msg = Message(agent_input=agent_input)
    result = agent.action_handler(msg)
    return _result_dict(result)


def _result_dict(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if hasattr(result, "dict"):
        return result.dict()
    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    return {}
