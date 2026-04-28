from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.assistant.lib.mcp.cache_refresher import refresh_server_tool_cache
from app.assistant.lib.tool_registry.mcp_discovery import get_discovered_tool
from app.assistant.lib.tool_registry.mcp_install_registry import McpInstallRecord, upsert_installed_tool
from app.assistant.lib.tool_registry.mcp_server_directory import load_mcp_server_directory
from app.assistant.lib.tool_registry.mcp_tool_cache import load_mcp_tool_cache
from app.assistant.lib.tool_registry.mcp_trust_policy import require_trusted_mcp_server
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class McpInstallResult:
    record: McpInstallRecord
    cache_refreshed: bool
    cache_path: str | None
    ready_for_planner: bool


def install_mcp_tool(
    *,
    tool_registry,
    server_id: str,
    tool_name: str,
    launch_id: Optional[str] = None,
    timeout_s: float = 15.0,
    refresh_cache_if_missing: bool = True,
    install_source: str = "manual",
) -> McpInstallResult:
    sid = (server_id or "").strip()
    tname = (tool_name or "").strip()
    if not sid:
        raise ValueError("server_id is required.")
    if not tname:
        raise ValueError("tool_name is required.")
    require_trusted_mcp_server(sid, context="MCP install")

    directory = load_mcp_server_directory()
    entry = (directory.entries_by_id or {}).get(sid)
    if not isinstance(entry, dict):
        raise ValueError(f"Unknown MCP server_id: {sid}")

    cache_path = None
    refreshed = False
    cached = load_mcp_tool_cache(sid)
    in_cache = any(isinstance(t, dict) and str(t.get("name") or "").strip() == tname for t in cached.tools)

    if not in_cache and refresh_cache_if_missing:
        refreshed_path = refresh_server_tool_cache(
            server_id=sid,
            server_entry=entry,
            launch_id=launch_id,
            timeout_s=timeout_s,
        )
        cache_path = str(refreshed_path)
        refreshed = True
        cached = load_mcp_tool_cache(sid)
        in_cache = any(isinstance(t, dict) and str(t.get("name") or "").strip() == tname for t in cached.tools)

    if not in_cache:
        raise ValueError(
            f"MCP tool '{tname}' not found in cache for server '{sid}'. "
            f"Refresh cache first or verify server allow/deny filters."
        )

    namespaced = f"mcp::{sid}::{tname}"
    record = upsert_installed_tool(
        server_id=sid,
        tool_name=tname,
        namespaced_tool_name=namespaced,
        install_source=install_source,
        enabled=True,
    )

    # Make the tool immediately available in current runtime without restart.
    # We only register this installed tool from cache.
    if hasattr(tool_registry, "load_installed_mcp_tools"):
        tool_registry.load_installed_mcp_tools(enabled_only=True)

    discovered = get_discovered_tool(server_id=sid, tool_name=tname)
    ready = bool(discovered is not None and discovered.installed)
    logger.info("Installed MCP tool %s (ready=%s)", namespaced, ready)
    return McpInstallResult(
        record=record,
        cache_refreshed=refreshed,
        cache_path=cache_path,
        ready_for_planner=ready,
    )
