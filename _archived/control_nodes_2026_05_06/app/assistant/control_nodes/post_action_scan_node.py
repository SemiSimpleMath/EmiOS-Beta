from __future__ import annotations

import time

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.pipeline_state import (
    get_flag,
    set_flag,
    set_pending_tool,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PostActionScanNode(ControlNode):
    """
    Deterministic "action + scan" accelerator for Playwright browsing.

    Goal:
    - After the planner executes an action tool (click/type/navigate/etc), immediately take ONE
      `browser_snapshot` before giving control back to the planner.
    - This avoids a wasted LLM cycle where the planner spends time deciding to scan.

    Contract:
    - ToolResultHandler schedules this node by setting:
        - next_agent = "post_action_scan_node"
        - playwright_auto_scan_in_progress = True
      and preserving the calling_agent in pipeline flags.
    - This node sets up the MCP snapshot tool call and routes to tool_caller.
    """

    SNAPSHOT_TOOL = "mcp::npm/playwright-mcp::browser_snapshot"

    def action_handler(self, message):  # noqa: ARG002
        # Always clear next_agent at start of control nodes
        try:
            self.blackboard.update_state_value("next_agent", None)
        except Exception as e:
            logger.error("[%s] Failed to clear next_agent at node start: %s", self.name, e)
            logger.debug("[%s] next_agent clear exception details", self.name, exc_info=True)
            raise

        tool_cfg = None
        try:
            tool_cfg = self.tool_registry.get_tool(self.SNAPSHOT_TOOL)
        except Exception as e:
            logger.error("[%s] Failed to resolve snapshot tool '%s': %s", self.name, self.SNAPSHOT_TOOL, e)
            logger.debug("[%s] snapshot tool lookup exception details", self.name, exc_info=True)
            tool_cfg = None

        calling_agent = get_flag(self.blackboard, "playwright_auto_scan_calling_agent", None)
        if not isinstance(calling_agent, str) or not calling_agent.strip():
            msg = (
                f"[{self.name}] Missing playwright_auto_scan_calling_agent while scheduling "
                f"{self.SNAPSHOT_TOOL}."
            )
            self.blackboard.update_state_value("error", True)
            self.blackboard.update_state_value("error_message", msg)
            logger.error(msg)
            raise ValueError(msg)
        calling_agent = calling_agent.strip()

        if not tool_cfg:
            msg = f"[{self.name}] Required snapshot tool missing: {self.SNAPSHOT_TOOL}"
            self.blackboard.update_state_value("error", True)
            self.blackboard.update_state_value("error_message", msg)
            logger.error(msg)
            raise ValueError(msg)

        # Small deterministic settle delay before snapshot.
        # DoorDash (and many JS-heavy sites) update UI asynchronously after actions;
        # taking a snapshot immediately can capture the "old" state.
        trigger_tool = get_flag(self.blackboard, "playwright_auto_scan_trigger_tool", None)

        # Default delays (seconds). Keep short to preserve speed.
        delay_s = 0.25
        if isinstance(trigger_tool, str) and trigger_tool.endswith("::browser_type"):
            delay_s = 0.8
        elif isinstance(trigger_tool, str) and trigger_tool.endswith("::browser_press_key"):
            delay_s = 0.6
        elif isinstance(trigger_tool, str) and trigger_tool.endswith("::browser_navigate"):
            delay_s = 0.8
        elif isinstance(trigger_tool, str) and trigger_tool.endswith("::browser_click"):
            delay_s = 0.4

        try:
            if delay_s and delay_s > 0:
                time.sleep(float(delay_s))
        except Exception as e:
            logger.debug("[%s] Delay sleep failed: %s", self.name, e, exc_info=True)

        # Set up the tool call deterministically (no ToolArguments LLM step).
        try:
            set_flag(self.blackboard, "playwright_auto_scan_prev_tool", trigger_tool)
        except Exception as e:
            logger.debug("[%s] Failed to set playwright_auto_scan_prev_tool: %s", self.name, e, exc_info=True)

        try:
            set_pending_tool(
                self.blackboard,
                name=self.SNAPSHOT_TOOL,
                calling_agent=calling_agent,
                action_input=None,
                arguments={},
                kind="tool",
            )
            self.blackboard.update_state_value("next_agent", "tool_caller")
            self.blackboard.update_state_value("last_agent", self.name)
        except Exception as e:
            logger.error("[%s] Failed to schedule snapshot tool call: %s", self.name, e)
            logger.debug("[%s] snapshot scheduling exception details", self.name, exc_info=True)
            self.blackboard.update_state_value("error", True)
            self.blackboard.update_state_value("error_message", str(e))
            raise

