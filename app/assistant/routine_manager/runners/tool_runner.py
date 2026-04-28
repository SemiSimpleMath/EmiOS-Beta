from __future__ import annotations

from typing import Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import ToolMessage

from .types import RoutineLike
from app.assistant.routine_manager.run_types import RoutineRunContext, RoutineRunResult


class ToolRoutineRunner:
    def run(self, routine: RoutineLike, run_ctx: RoutineRunContext) -> RoutineRunResult:
        # target_date is not automatically injected; pass it explicitly in spec.arguments if desired.
        spec = routine.spec or {}
        tool_name = str(spec.get("tool_name") or "").strip()
        if not tool_name:
            raise ValueError("tool runner requires spec.tool_name")

        tool_registry = getattr(DI, "tool_registry", None)
        if tool_registry is None:
            raise RuntimeError("Tool registry not available")
        tool_class = tool_registry.get_tool_class(tool_name)
        if tool_class is None:
            raise ValueError(f"Unknown or unsupported tool: {tool_name}")
        tool = tool_class()

        tool_message = ToolMessage(
            tool_name=tool_name,
            tool_data={
                "tool_name": tool_name,
                "arguments": spec.get("arguments") or {},
            },
        )
        result = tool.execute(tool_message)
        if result is not None and getattr(result, "result_type", None) == "error":
            error_content = getattr(result, "content", "Tool returned an error")
            raise RuntimeError(f"Tool '{tool_name}' returned error: {error_content}")
        return RoutineRunResult(status="success", data={"tool_name": tool_name})
