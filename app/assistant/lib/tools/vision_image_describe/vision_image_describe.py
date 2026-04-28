from __future__ import annotations

from pathlib import Path
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ToolMessage, ToolResult
from app.assistant.utils.time_utils import get_local_time_str

logger = get_logger(__name__)


class VisionImageDescribeTool(BaseTool):
    """
    Run shared::vision_image_describe on a provided image path.
    """

    AGENT = "shared::vision_image_describe"

    def __init__(self):
        super().__init__("vision_image_describe")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data
        image_path = (args.get("image_path") or "").strip()
        question = (args.get("question") or "").strip()

        if not image_path:
            return ToolResult(result_type="error", content="Missing required argument: image_path")

        path = Path(image_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return ToolResult(result_type="error", content=f"Image not found: {path}")

        agent = DI.agent_factory.create_agent(self.AGENT)
        if agent is None:
            return ToolResult(
                result_type="error",
                content=f"vision_image_describe error: could not create agent {self.AGENT}",
                data={"agent": self.AGENT},
            )

        task = question or "Describe this image concisely."
        msg = Message(
            agent_input={
                "date_time": get_local_time_str(),
                "task": task,
                "image": str(path),
            }
        )
        res = agent.action_handler(msg)

        payload: dict[str, Any]
        if hasattr(res, "data") and isinstance(res.data, dict):
            payload = res.data
        elif isinstance(res, dict):
            payload = res
        else:
            payload = {"raw": str(res)}

        description = ""
        if isinstance(payload.get("description"), str):
            description = payload.get("description") or ""
        elif isinstance(payload.get("page_overview"), str):
            description = payload.get("page_overview") or ""
        else:
            description = str(payload.get("raw") or "").strip()

        # ToolResult.content should be the main short text.
        content = description.strip()

        data = {
            "image_path": str(path),
            "question": task,
            "describe": payload,
        }

        return ToolResult(
            result_type="vision_image_describe",
            content=content,
            data=data,
        )


def get_tool_class():
    return VisionImageDescribeTool

