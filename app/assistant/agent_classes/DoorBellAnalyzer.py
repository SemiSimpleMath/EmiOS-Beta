"""DoorBellAnalyzer — captions Ring frames captured by the front-door camera.

Wakes on ``ring_snapshot_captured`` events fired by the smart_home_bridge
after it writes a fresh JPEG to ``data/ring_snapshots/``. Filters by
``camera_ids`` from config so only doorbell-camera frames are processed.
Sends each frame to a vision-capable LLM, gets a short caption +
significance flag, and writes the caption as a ``.txt`` sidecar next to
the JPEG.

Pod creation when ``is_significant=True`` is a TODO — for now we just log
the verdict.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from app.assistant.agent_classes.Agent import Agent
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)


class DoorBellAnalyzer(Agent):
    def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None, parent=None):
        super().__init__(name, blackboard, agent_registry, tool_registry, llm_params, parent)

    def _allowed_camera_ids(self) -> List[str]:
        raw = self.config.get("camera_ids") or []
        if not isinstance(raw, list):
            return []
        return [str(c).strip() for c in raw if str(c).strip()]

    def ring_snapshot_captured_handler(self, message: Message):
        try:
            payload = self._extract_payload(message)
            camera_id = str(payload.get("camera_id") or "").strip()
            allowed = self._allowed_camera_ids()
            if allowed and camera_id not in allowed:
                # Snapshot is from a different camera; not our concern.
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
                logger.info("[%s] caption already exists for %s; skipping", self.name, jpeg.name)
                return

            agent_input = {
                "date_time": get_local_time_str(),
                "image": str(jpeg),
                "camera_id": camera_id,
                "captured_at_utc": str(payload.get("captured_at_utc") or ""),
            }
            llm_msg = Message(agent_input=agent_input)
            result = self.action_handler(llm_msg)

            data = self._extract_result_data(result)
            caption = str(data.get("caption") or "").strip()
            is_significant = bool(data.get("is_significant"))
            significance_reason = str(data.get("significance_reason") or "").strip()

            if not caption:
                logger.warning("[%s] LLM returned no caption for %s", self.name, jpeg.name)
                return

            self._write_sidecar(sidecar, caption, is_significant, significance_reason, agent_input)
            logger.info(
                "[%s] captioned %s (significant=%s)",
                self.name, jpeg.name, is_significant,
            )

            if is_significant:
                # TODO: mint a pod from this snapshot — kind=image, body=caption,
                # link to camera_id + captured_at_utc. Hold for now per user request.
                logger.info(
                    "[%s] SIGNIFICANT (pod creation deferred): %s — %s",
                    self.name, jpeg.name, significance_reason or "(no reason given)",
                )

        except Exception as e:
            logger.error("[%s] handler crashed: %s", self.name, e)
            logger.debug("[%s] handler exception details", self.name, exc_info=True)

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
        caption: str,
        is_significant: bool,
        significance_reason: str,
        agent_input: Dict[str, Any],
    ) -> None:
        lines = [
            caption,
            "",
            f"camera_id: {agent_input.get('camera_id', '')}",
            f"captured_at_utc: {agent_input.get('captured_at_utc', '')}",
            f"analyzed_at_local: {agent_input.get('date_time', '')}",
            f"analyzer: door_bell_analyzer",
            f"is_significant: {str(is_significant).lower()}",
        ]
        if significance_reason:
            lines.append(f"significance_reason: {significance_reason}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
