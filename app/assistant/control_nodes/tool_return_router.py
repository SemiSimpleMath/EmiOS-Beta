from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import get_last_tool_result_meta, set_resume_target

logger = get_logger(__name__)


class ToolReturnRouter(ControlNode):
    """
    Deterministic post-tool router.

    Responsibilities:
    - Validate the source node that produced the tool result.
    - Capture resume target (tool calling agent) for downstream nodes.
    - Route directly back to the tool calling agent.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        tool_call_result_handler_node = self._required_str(cfg, "tool_call_result_handler_node")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != tool_call_result_handler_node:
            raise ValueError(
                f"[{self.name}] expected last_agent={tool_call_result_handler_node} before tool return route, got: {last_agent!r}"
            )

        meta = get_last_tool_result_meta(self.blackboard) or {}
        calling_agent = meta.get("calling_agent")
        if not isinstance(calling_agent, str) or not calling_agent.strip():
            raise ValueError(f"[{self.name}] Missing last_tool_result_meta.calling_agent.")
        calling_agent = calling_agent.strip()
        set_resume_target(self.blackboard, calling_agent)

        # Do NOT set next_agent here. Let the state_map route through
        # downstream processing nodes (page_state, summary, critic, etc.)
        # before returning to the planner. The resume_target tells those
        # nodes where to eventually route back to.
        self.blackboard.update_state_value("last_agent", self.name)

    def _cfg(self) -> dict:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        tool_return = flow_cfg.get("tool_return")
        if not isinstance(tool_return, dict):
            raise ValueError("manager_flow_config.tool_return must be a dict.")
        return tool_return

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"tool return router requires non-empty '{key}'.")
        return raw.strip()

