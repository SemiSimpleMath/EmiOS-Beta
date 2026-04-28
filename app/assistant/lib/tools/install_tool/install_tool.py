from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tool_registry.mcp_installer import install_mcp_tool
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


def _parse_namespaced_mcp_tool(tool_name: str) -> Optional[Tuple[str, str]]:
    raw = (tool_name or "").strip()
    if not raw:
        return None
    if not raw.startswith("mcp::"):
        return None
    parts = raw.split("::", 2)
    if len(parts) != 3:
        raise ValueError(
            f"Invalid MCP namespaced tool_name '{raw}'. Expected format: mcp::<server_id>::<tool_name>"
        )
    server_id = parts[1].strip()
    mcp_tool_name = parts[2].strip()
    if not server_id or not mcp_tool_name:
        raise ValueError(
            f"Invalid MCP namespaced tool_name '{raw}'. server_id and tool_name must be non-empty."
        )
    return server_id, mcp_tool_name


def _normalize_server_and_tool(
    *,
    server_id: str,
    mcp_tool_name: str,
) -> Tuple[str, str]:
    sid = (server_id or "").strip()
    tname = (mcp_tool_name or "").strip()
    if not sid:
        return sid, tname

    if not sid.startswith("mcp::"):
        return sid, tname

    # Accept both:
    # - mcp::<server_id>
    # - mcp::<server_id>::<tool_name>
    parts = sid.split("::", 2)
    if len(parts) < 2:
        raise ValueError(
            f"Invalid server_id '{sid}'. Expected 'mcp::<server_id>' or bare '<server_id>'."
        )
    normalized_sid = parts[1].strip()
    if not normalized_sid:
        raise ValueError(
            f"Invalid server_id '{sid}'. Server id is empty after 'mcp::' prefix."
        )

    if len(parts) == 3:
        sid_tool = parts[2].strip()
        if sid_tool:
            if tname and tname != sid_tool:
                raise ValueError(
                    "Conflicting tool identifiers: "
                    f"server_id='{sid}' implies tool '{sid_tool}', "
                    f"but mcp_tool_name='{tname}'."
                )
            tname = sid_tool

    return normalized_sid, tname


class InstallToolTool(BaseTool):
    """
    Install a trusted MCP tool and register it in runtime.
    """
    requires_approval = True

    def __init__(self):
        super().__init__("install_tool")

    def describe_action(self, tool_message: ToolMessage) -> tuple[str, str]:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data
        tool_name = str(args.get("tool_name") or "").strip()
        server_id = str(args.get("server_id") or "").strip()
        mcp_tool_name = str(args.get("mcp_tool_name") or "").strip()
        target = tool_name or f"mcp::{server_id}::{mcp_tool_name}".strip(":")
        return (
            "Allow install tool?",
            f"Install MCP tool request: {target}",
        )

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data

        tool_name = str(args.get("tool_name") or "").strip()
        server_id = str(args.get("server_id") or "").strip()
        mcp_tool_name = str(args.get("mcp_tool_name") or "").strip()
        install_source = str(args.get("install_source") or "agent").strip()
        launch_id = str(args.get("launch_id") or "").strip() or None
        timeout_s = float(args.get("timeout_s") or 15.0)

        # If the tool already exists in the registry, treat install_tool as
        # a visibility action and return success without install side effects.
        if tool_name and not tool_name.startswith("mcp::"):
            existing_cfg = DI.tool_registry.get_tool(tool_name)
            if isinstance(existing_cfg, dict):
                return ToolResult(
                    result_type="tool_result",
                    content=f"Tool already available: {tool_name}. Marked visible for this run.",
                    data={
                        "tool_name": tool_name,
                        "already_available": True,
                        "cache_refreshed": False,
                        "ready_for_planner": True,
                    },
                )

        parsed = _parse_namespaced_mcp_tool(tool_name) if tool_name else None
        if parsed is not None:
            server_id, mcp_tool_name = parsed
        else:
            server_id, mcp_tool_name = _normalize_server_and_tool(
                server_id=server_id,
                mcp_tool_name=mcp_tool_name,
            )

        if not server_id or not mcp_tool_name:
            raise ValueError(
                "install_tool requires either tool_name='mcp::<server_id>::<tool_name>' "
                "or explicit server_id + mcp_tool_name."
            )

        try:
            result = install_mcp_tool(
                tool_registry=DI.tool_registry,
                server_id=server_id,
                tool_name=mcp_tool_name,
                launch_id=launch_id,
                timeout_s=timeout_s,
                install_source=install_source,
            )
        except Exception as e:
            logger.error(
                "install_tool failed for server_id='%s', tool_name='%s': %s",
                server_id,
                mcp_tool_name,
                e,
            )
            logger.debug("install_tool exception details", exc_info=True)
            raise

        namespaced = result.record.namespaced_tool_name
        return ToolResult(
            result_type="tool_result",
            content=f"Installed MCP tool: {namespaced}",
            data={
                "tool_name": namespaced,
                "server_id": result.record.server_id,
                "mcp_tool_name": result.record.tool_name,
                "installed_at_utc": result.record.installed_at_utc,
                "install_source": result.record.install_source,
                "enabled": result.record.enabled,
                "cache_refreshed": result.cache_refreshed,
                "cache_path": result.cache_path,
                "ready_for_planner": result.ready_for_planner,
            },
        )


def get_tool_class():
    return InstallToolTool
