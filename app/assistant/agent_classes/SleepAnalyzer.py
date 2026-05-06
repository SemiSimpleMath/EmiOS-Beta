"""SleepAnalyzer — analyzes Ring frames captured of the user's bed.

Wakes on ``ring_snapshot_captured`` events, filters to the bed-facing
camera (per ``camera_ids`` in config), and asks a vision-LLM what's
visible: subject in bed, position, motion vs the previous frame,
lighting, awake indicators. Writes a structured ``.txt`` sidecar next
to the JPEG.

Per-frame analysis only. Aggregation into a nightly sleep-quality report
is a separate concern (a routine that walks the sidecars and summarizes
the night) — TODO.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from app.assistant.agent_classes.Agent import Agent
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)


class SleepAnalyzer(Agent):
    def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None, parent=None):
        super().__init__(name, blackboard, agent_registry, tool_registry, llm_params, parent)

    def _allowed_camera_ids(self) -> List[str]:
        raw = self.config.get("camera_ids") or []
        if not isinstance(raw, list):
            return []
        return [str(c).strip() for c in raw if str(c).strip()]

    # Maximum gap between current and previous frame for comparison to be
    # meaningful. Beyond this, we treat the current frame as the first of a
    # new sleep window and analyze it solo. Generous so a single missed tick
    # doesn't disable comparison.
    _MAX_PAIR_GAP_SECONDS = 10 * 60  # 10 minutes

    def ring_snapshot_captured_handler(self, message: Message):
        try:
            payload = self._extract_payload(message)
            camera_id = str(payload.get("camera_id") or "").strip()
            allowed = self._allowed_camera_ids()
            if allowed and camera_id not in allowed:
                return

            snapshot_path = str(payload.get("snapshot_path") or "").strip()
            if not snapshot_path:
                logger.warning("[%s] event missing snapshot_path; skipping", self.name)
                return
            jpeg = Path(snapshot_path)
            if not jpeg.exists():
                logger.warning("[%s] snapshot vanished before analysis: %s", self.name, jpeg)
                return

            sidecar = jpeg.with_suffix(".txt")
            if sidecar.exists():
                logger.info("[%s] analysis already exists for %s; skipping", self.name, jpeg.name)
                return

            previous_jpeg = self._find_previous_frame(jpeg, camera_id)
            previous_label = previous_jpeg.name if previous_jpeg else "(none — first frame in window)"

            agent_input = {
                "date_time": get_local_time_str(),
                "image": str(jpeg),
                "previous_image": str(previous_jpeg) if previous_jpeg else "",
                "previous_image_label": previous_label,
                "has_previous": bool(previous_jpeg),
                "camera_id": camera_id,
                "captured_at_utc": str(payload.get("captured_at_utc") or ""),
            }
            llm_msg = Message(agent_input=agent_input)
            result = self.action_handler(llm_msg)

            data = self._extract_result_data(result)
            if not data:
                logger.warning("[%s] LLM returned no analysis for %s", self.name, jpeg.name)
                return

            self._write_sidecar(sidecar, data, agent_input)
            logger.info(
                "[%s] analyzed %s — in_bed=%s pos=%s motion=%s importance=%s",
                self.name, jpeg.name,
                data.get("subject_in_bed"),
                data.get("position"),
                data.get("motion_vs_previous"),
                data.get("importance"),
            )

        except Exception as e:
            logger.error("[%s] handler crashed: %s", self.name, e)
            logger.debug("[%s] handler exception details", self.name, exc_info=True)

    def _find_previous_frame(self, current: Path, camera_id: str) -> Path | None:
        """Return the most recent JPEG for the same camera that's older than
        ``current`` and within ``_MAX_PAIR_GAP_SECONDS``. Returns None if
        no such frame exists (first frame of a window, or last one is too old).
        """
        try:
            snap_dir = current.parent
            current_mtime = current.stat().st_mtime
            best: Path | None = None
            best_mtime = -1.0
            cam_suffix = f"_{camera_id}.jpg"
            for p in snap_dir.iterdir():
                if not p.is_file() or p == current:
                    continue
                if not p.name.endswith(cam_suffix):
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
            if best is None:
                return None
            if (current_mtime - best_mtime) > self._MAX_PAIR_GAP_SECONDS:
                return None
            return best
        except Exception as e:
            logger.warning("[%s] previous-frame lookup failed: %s", self.name, e)
            return None

    @staticmethod
    def _extract_payload(message: Message) -> Dict[str, Any]:
        for attr in ("data", "tool_data", "metadata"):
            v = getattr(message, attr, None)
            if isinstance(v, dict) and v:
                return v
        ai = getattr(message, "agent_input", None)
        return ai if isinstance(ai, dict) else {}

    @staticmethod
    def _extract_result_data(result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            return result
        if hasattr(result, "model_dump"):
            return result.model_dump()
        if hasattr(result, "dict"):
            return result.dict()
        if hasattr(result, "data") and isinstance(result.data, dict):
            return result.data
        return {}

    @staticmethod
    def _write_sidecar(
        path: Path,
        data: Dict[str, Any],
        agent_input: Dict[str, Any],
    ) -> None:
        notes = str(data.get("notes") or "").strip()
        awake_indicators = data.get("awake_indicators") or []
        if isinstance(awake_indicators, list):
            awake_str = "; ".join(str(x).strip() for x in awake_indicators if str(x).strip())
        else:
            awake_str = str(awake_indicators).strip()

        # Clamp importance to [0, 10] defensively in case the LLM returns
        # something out of range or a non-int.
        try:
            importance = int(data.get("importance"))
        except (TypeError, ValueError):
            importance = 0
        importance = max(0, min(10, importance))
        importance_reason = str(data.get("importance_reason") or "").strip()

        lines = [
            notes if notes else "(no notes)",
            "",
            f"camera_id: {agent_input.get('camera_id', '')}",
            f"captured_at_utc: {agent_input.get('captured_at_utc', '')}",
            f"analyzed_at_local: {agent_input.get('date_time', '')}",
            f"analyzer: sleep_analyzer",
            f"previous_image: {agent_input.get('previous_image_label', '')}",
            f"subject_in_bed: {str(bool(data.get('subject_in_bed'))).lower()}",
            f"position: {str(data.get('position') or 'unclear').strip()}",
            f"motion_vs_previous: {str(data.get('motion_vs_previous') or 'unclear').strip()}",
            f"light_state: {str(data.get('light_state') or 'unclear').strip()}",
            f"awake_indicators: {awake_str}",
            f"importance: {importance}",
        ]
        if importance_reason:
            lines.append(f"importance_reason: {importance_reason}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
