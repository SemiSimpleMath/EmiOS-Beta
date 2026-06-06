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


def resolve_tool_min_authority(tool_name: str, tool_config: dict | Any | None) -> int | None:
    """The L1 'see + use' authority floor for a tool.

    Returns the integer floor, or ``None`` when the tool has no floor — i.e.
    it carries no tool_contract at all (MCP/dynamic/core tools), in which case
    the allowed_tools ceiling is the only gate.

    A FIRST-PARTY tool that carries a contract but omits ``metadata.min_authority``
    fails CLOSED at 99: a forgotten floor must never silently become wide-open
    (matches the project's fail-loud stance). After the Phase-2 migration every
    first-party contract declares it, so 99 is a guard for future contracts, not a
    value any current tool relies on.
    """
    # MCP / dynamic tools carry a GENERATED contract that has no min_authority; they
    # are bounded by the allowed_tools ceiling, not an L1 floor. Exempt them so the
    # fail-closed 99 below applies only to first-party contracts that forgot the field
    # (an MCP tool must not be denied to every sub-100 scope just for lacking a floor).
    if isinstance(tool_name, str) and tool_name.startswith("mcp::"):
        return None
    if isinstance(tool_config, dict) and tool_config.get("backend") == "mcp":
        return None

    contract = None
    if isinstance(tool_config, dict) and isinstance(tool_config.get("tool_contract"), dict):
        contract = tool_config["tool_contract"]
    if contract is None:
        return None  # no contract -> ceiling-gated only (core/dynamic)
    metadata = contract.get("metadata") if isinstance(contract.get("metadata"), dict) else {}
    raw = metadata.get("min_authority")
    if raw is None:
        return 99  # contract present but floor omitted -> fail closed
    if isinstance(raw, bool):
        raise ValueError(f"[{tool_name}] tool_contract.metadata.min_authority must be an integer 0-100.")
    floor = int(raw)
    if floor < 0 or floor > 100:
        raise ValueError(f"[{tool_name}] tool_contract.metadata.min_authority must be between 0 and 100.")
    return floor


def check_tool_access(
    *,
    tool_name: str,
    scope_contract_enforced: bool,
    scope_context: ScopeContext | Any | None,
    task_allowed_tools: list | None,
    task_except_tools: list | None,
    caller_name: str,
    tool_min_authority: int | None = None,
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

        # --- L1 authority floor (min_authority): see+use gate ---
        # A tool is reachable only when the scope's authority clears the tool's
        # floor. This is the wall the Telegram breach lacked: a 40-authority
        # guest could reach personal_admin_manager (floor 90) because nothing
        # checked authority at the access layer. authority >= 100 (admin) clears
        # every floor naturally. None = tool has no floor (ceiling-gated only).
        if tool_min_authority is not None:
            authority_level = int(getattr(scope_context.approval, "authority_level", 0) or 0)
            if authority_level < int(tool_min_authority):
                return (
                    False,
                    f"Tool '{tool_name}' requires authority {int(tool_min_authority)}; "
                    f"this scope has {authority_level}.",
                )

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
