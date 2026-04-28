from __future__ import annotations

from typing import Any

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ToolScopeStateManager:
    """
    Shared runtime tool-scope state mutations.

    Centralizes updates to:
    - visible_tools
    - dynamic_allowed_tools
    - dynamic_denied_tools
    - recently_used_tools
    - recently_installed_tools
    - tool_visibility_max
    """

    # -1 means unlimited visibility (no top-N cap).
    DEFAULT_MAX_VISIBLE = -1
    MAX_TRACKED_TOOLS = 50

    def __init__(self, blackboard):
        self.blackboard = blackboard

    def _read_list(self, key: str) -> list[str]:
        try:
            raw = self.blackboard.get_state_value(key)
        except Exception as e:
            logger.debug("[ToolScopeStateManager] Failed reading '%s': %s", key, e, exc_info=True)
            raw = None
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        out: list[str] = []
        for item in items:
            if item not in out:
                out.append(item)
        return out

    @classmethod
    def _normalize_max_visible(cls, raw_value: Any) -> int:
        if raw_value is None:
            return -1
        try:
            max_visible = int(raw_value)
        except Exception as e:
            logger.debug("[ToolScopeStateManager] Invalid max visible value %r: %s", raw_value, e, exc_info=True)
            max_visible = cls.DEFAULT_MAX_VISIBLE
        if max_visible == 0:
            # Backward compatibility: previous 0=unlimited behavior.
            return -1
        if max_visible < -1:
            max_visible = -1
        return max_visible

    @staticmethod
    def _apply_limit(items: list[str], max_visible: int) -> list[str]:
        if max_visible == -1:
            return list(items)
        return list(items)[:max_visible]

    def _read_max_visible(self) -> int:
        try:
            raw = self.blackboard.get_state_value("tool_visibility_max")
        except Exception as e:
            logger.debug("[ToolScopeStateManager] Failed reading tool_visibility_max: %s", e, exc_info=True)
            raw = self.DEFAULT_MAX_VISIBLE
        return self._normalize_max_visible(raw)

    def initialize_runtime_scope(self, *, visible_tools: list[str], max_visible: int) -> None:
        clamped_max = self._normalize_max_visible(max_visible)
        dedup_visible = self._dedupe([x for x in visible_tools if isinstance(x, str) and x.strip()])
        self.blackboard.update_state_value("dynamic_allowed_tools", [])
        self.blackboard.update_state_value("dynamic_denied_tools", [])
        self.blackboard.update_state_value("recently_used_tools", [])
        self.blackboard.update_state_value("recently_installed_tools", [])
        self.blackboard.update_state_value("visible_tools", self._apply_limit(dedup_visible, clamped_max))
        self.blackboard.update_state_value("tool_visibility_max", clamped_max)

    def record_tool_result(
        self,
        *,
        selected_tool: str | None,
        installed_tool_name: str | None = None,
        skip_install_apply: bool = False,
    ) -> None:
        max_visible = self._read_max_visible()
        visible = self._read_list("visible_tools")
        dyn_allowed = self._read_list("dynamic_allowed_tools")
        recently_used = self._read_list("recently_used_tools")
        recently_installed = self._read_list("recently_installed_tools")

        if isinstance(selected_tool, str) and selected_tool.strip():
            tool_name = selected_tool.strip()
            if tool_name in visible:
                visible = [x for x in visible if x != tool_name]
            visible.insert(0, tool_name)
            if tool_name in recently_used:
                recently_used = [x for x in recently_used if x != tool_name]
            recently_used.insert(0, tool_name)

        installed_name = None
        if isinstance(installed_tool_name, str) and installed_tool_name.strip():
            installed_name = installed_tool_name.strip()
        if selected_tool == "install_tool" and not skip_install_apply and isinstance(installed_name, str):
            if installed_name in recently_installed:
                recently_installed = [x for x in recently_installed if x != installed_name]
            recently_installed.insert(0, installed_name)
            if installed_name in visible:
                visible = [x for x in visible if x != installed_name]
            visible.insert(0, installed_name)
            if installed_name.startswith("mcp::"):
                if installed_name in dyn_allowed:
                    dyn_allowed = [x for x in dyn_allowed if x != installed_name]
                dyn_allowed.insert(0, installed_name)

        self.blackboard.update_state_value("visible_tools", self._apply_limit(self._dedupe(visible), max_visible))
        self.blackboard.update_state_value("dynamic_allowed_tools", self._dedupe(dyn_allowed)[: self.MAX_TRACKED_TOOLS])
        self.blackboard.update_state_value("recently_used_tools", self._dedupe(recently_used)[: self.MAX_TRACKED_TOOLS])
        self.blackboard.update_state_value("recently_installed_tools", self._dedupe(recently_installed)[: self.MAX_TRACKED_TOOLS])

    def approve_installed_tool(self, *, namespaced_tool_name: str) -> None:
        if not isinstance(namespaced_tool_name, str) or not namespaced_tool_name.startswith("mcp::"):
            raise ValueError("approve_installed_tool requires a namespaced MCP tool name.")
        max_visible = self._read_max_visible()
        visible = self._read_list("visible_tools")
        dyn_allowed = self._read_list("dynamic_allowed_tools")
        recently_installed = self._read_list("recently_installed_tools")

        dyn_allowed.insert(0, namespaced_tool_name)
        recently_installed.insert(0, namespaced_tool_name)
        if namespaced_tool_name in visible:
            visible = [x for x in visible if x != namespaced_tool_name]
        visible.insert(0, namespaced_tool_name)

        self.blackboard.update_state_value("dynamic_allowed_tools", self._dedupe(dyn_allowed)[: self.MAX_TRACKED_TOOLS])
        self.blackboard.update_state_value("recently_installed_tools", self._dedupe(recently_installed)[: self.MAX_TRACKED_TOOLS])
        self.blackboard.update_state_value("visible_tools", self._apply_limit(self._dedupe(visible), max_visible))
