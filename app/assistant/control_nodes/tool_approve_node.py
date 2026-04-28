from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.manager_runtime.services.tool_scope_state_manager import ToolScopeStateManager
from app.assistant.lib.tool_registry.mcp_install_registry import list_installed_records
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ToolApproveNode(ControlNode):
    """
    Manager-scoped approval/gating for newly installed tools.

    This node does not mutate static manager config. It only updates runtime
    blackboard state:
      - dynamic_allowed_tools
      - recently_installed_tools
      - visible_tools
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        pending = self.blackboard.get_state_value("pending_tool_approval")
        if not isinstance(pending, dict):
            raise RuntimeError(f"[{self.name}] Missing pending_tool_approval payload.")

        calling_agent = pending.get("calling_agent")
        namespaced = pending.get("tool_name")
        if not isinstance(calling_agent, str) or not calling_agent.strip():
            raise RuntimeError(f"[{self.name}] Missing calling_agent in pending_tool_approval payload.")
        if not isinstance(namespaced, str) or not namespaced.startswith("mcp::"):
            raise RuntimeError(f"[{self.name}] Invalid pending tool for approval: {namespaced!r}")

        # Strict approval basis: tool must be present in install registry and enabled.
        installed = {
            str(r.namespaced_tool_name).strip()
            for r in list_installed_records(enabled_only=True)
            if isinstance(getattr(r, "namespaced_tool_name", None), str) and str(r.namespaced_tool_name).strip()
        }
        if namespaced not in installed:
            raise RuntimeError(
                f"[{self.name}] Tool '{namespaced}' is not present in enabled install registry; refusing approval."
            )

        ToolScopeStateManager(self.blackboard).approve_installed_tool(namespaced_tool_name=namespaced)
        self.blackboard.update_state_value(
            "last_tool_approval",
            {
                "tool_name": namespaced,
                "approved": True,
                "approved_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )
        self.blackboard.update_state_value("pending_tool_approval", None)
        self.blackboard.update_state_value("last_agent", self.name)
        self.blackboard.update_state_value("next_agent", calling_agent)


