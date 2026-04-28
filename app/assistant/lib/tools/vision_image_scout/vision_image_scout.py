from __future__ import annotations

from pathlib import Path
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolMessage, ToolResult
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)


class VisionImageScoutTool(BaseTool):
    """
    Run shared::vision_prose_scout on a provided image path and return prose.
    """

    SCOUT_AGENT = "shared::vision_prose_scout"

    def __init__(self):
        super().__init__("vision_image_scout")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments", {}) or {}
        image_path = (args.get("image_path") or "").strip()
        question = (args.get("question") or "").strip()

        if not image_path:
            return ToolResult(result_type="error", content="Missing required argument: image_path")

        path = Path(image_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return ToolResult(result_type="error", content=f"Image not found: {path}")

        scout = DI.agent_factory.create_agent(self.SCOUT_AGENT)
        if scout is None:
            return ToolResult(
                result_type="error",
                content=f"vision_image_scout error: could not create agent {self.SCOUT_AGENT}",
                data={"agent": self.SCOUT_AGENT},
            )

        task = question or "Describe this image in 1-2 sentences."
        msg = Message(
            agent_input={
                "date_time": get_local_time_str(),
                "task": task,
                "image": str(path),
            }
        )
        res = scout.action_handler(msg)

        payload: dict[str, Any]
        if hasattr(res, "data") and isinstance(res.data, dict):
            payload = res.data
        elif isinstance(res, dict):
            payload = res
        else:
            payload = {"raw": str(res)}

        overview = ""
        if isinstance(payload.get("page_overview"), str):
            overview = payload.get("page_overview") or ""
        elif isinstance(payload.get("description"), str):
            overview = payload.get("description") or ""
        else:
            overview = payload.get("raw") or ""

        content = overview.strip() if isinstance(overview, str) else str(overview)

        return ToolResult(
            result_type="vision_image_scout",
            content=content,
            data={
                "image_path": str(path),
                "question": task,
                "scout": payload,
            },
        )


def get_tool_class():
    return VisionImageScoutTool
