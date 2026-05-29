"""
Tool access control for ToolCaller.

Two-layer restriction:
1. Scope contract (allowed_tools / blocked_tools from ScopeContext)
2. Task-level (task_allowed_tools / task_except_tools from blackboard)

Plus a dynamic policy: if install_tool is in the task allow-list,
already-installed MCP tools are auto-permitted in the same run.
"""
from __future__ import annotations

from typing import Any

from app.assistant.lib.tool_registry.mcp_install_registry import list_installed_records
from app.assistant.utils.pydantic_classes import ScopeContext
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def check_tool_access(
    *,
    tool_name: str,
    scope_contract_enforced: bool,
    scope_context: ScopeContext | Any | None,
    task_allowed_tools: list | None,
    task_except_tools: list | None,
    caller_name: str,
) -> tuple[bool, str]:
    """Check whether *tool_name* is permitted.

    Returns ``(allowed, reason)``.  When ``allowed`` is False, *reason*
    is a human-readable error message suitable for the blackboard.

    Note: ``always_show`` is a visibility-only concept (narrower-bypass
    in tool_scope_service). It does NOT grant permission and is not
    consulted here — permission flows purely from scope.allowed_tools
    intersected with task-level restrictions.
    """
    # --- Layer 1: Scope contract ---
    if scope_contract_enforced and isinstance(scope_context, ScopeContext):
        scope_allowed = scope_context.tools.allowed_tools if isinstance(scope_context.tools.allowed_tools, list) else []
        scope_blocked = scope_context.tools.blocked_tools if isinstance(scope_context.tools.blocked_tools, list) else []
        scope_allowset = {str(x).strip() for x in scope_allowed if isinstance(x, str) and str(x).strip()}
        if "all" in scope_allowset and len(scope_allowset) > 1:
            raise ValueError(
                f"[{caller_name}] scope_context.tools.allowed_tools cannot mix 'all' with specific tool names."
            )
        if "all" not in scope_allowset and tool_name not in scope_allowset:
            return False, f"Tool '{tool_name}' is outside scope_contract allowed_tools."
        if tool_name in set(scope_blocked):
            return False, f"Tool '{tool_name}' is blocked by scope_contract."

    # --- Layer 2: Task-level restrictions ---
    allowset = None
    denyset: set[str] = set()
    if isinstance(task_allowed_tools, list):
        allowset = {str(x).strip() for x in task_allowed_tools if isinstance(x, str) and x.strip()}
        if "all" in allowset and len(allowset) > 1:
            raise ValueError(f"[{caller_name}] task_allowed_tools cannot mix 'all' with specific tool names.")
    if isinstance(task_except_tools, list) and task_except_tools:
        denyset = {str(x).strip() for x in task_except_tools if isinstance(x, str) and x.strip()}

    # Dynamic policy: if install_tool is allowed, auto-permit installed MCP tools.
    if (
        allowset is not None
        and "all" not in allowset
        and tool_name not in allowset
        and isinstance(tool_name, str)
        and tool_name.startswith("mcp::")
        and "install_tool" in allowset
    ):
        try:
            installed_names = {
                str(r.namespaced_tool_name).strip()
                for r in list_installed_records(enabled_only=True)
                if isinstance(getattr(r, "namespaced_tool_name", None), str)
                and str(r.namespaced_tool_name).strip()
            }
        except Exception:
            logger.debug("[%s] Could not list installed MCP tool records", caller_name, exc_info=True)
            installed_names = set()
        if tool_name in installed_names:
            allowset = set(allowset)
            allowset.add(tool_name)

    if allowset is not None and "all" not in allowset and tool_name not in allowset:
        return False, f"Tool '{tool_name}' is not allowed for this task."
    if tool_name in denyset:
        return False, f"Tool '{tool_name}' is denied for this task."

    return True, ""
