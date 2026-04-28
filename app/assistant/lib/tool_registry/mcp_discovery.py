from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.assistant.lib.tool_registry.mcp_install_registry import list_installed_records
from app.assistant.lib.tool_registry.mcp_server_directory import load_mcp_server_directory
from app.assistant.lib.tool_registry.mcp_tool_cache import load_mcp_tool_cache
from app.assistant.lib.tool_registry.mcp_trust_policy import is_trusted_mcp_server, require_trusted_mcp_server


@dataclass(frozen=True)
class McpDiscoveredTool:
    namespaced_tool_name: str
    server_id: str
    tool_name: str
    description: str
    input_schema: dict[str, Any]
    installed: bool
    source: str


def discover_mcp_tools(
    *,
    query: str,
    limit: int = 20,
    include_disabled_servers: bool = False,
) -> list[McpDiscoveredTool]:
    q = (query or "").strip().lower()
    if not q:
        raise ValueError("query must be a non-empty string.")
    if limit <= 0:
        raise ValueError("limit must be > 0.")

    directory = load_mcp_server_directory()
    installed = {(r.server_id, r.tool_name) for r in list_installed_records(enabled_only=True)}
    out: list[McpDiscoveredTool] = []

    for server_id, entry in (directory.entries_by_id or {}).items():
        if not is_trusted_mcp_server(server_id):
            continue
        enabled = bool(entry.get("enabled", False))
        if not include_disabled_servers and not enabled:
            continue

        cache = load_mcp_tool_cache(server_id)
        for t in cache.tools:
            if not isinstance(t, dict):
                continue
            tool_name = t.get("name")
            if not isinstance(tool_name, str) or not tool_name.strip():
                continue
            desc = str(t.get("description") or "")
            haystack = f"{server_id} {tool_name} {desc}".lower()
            if q not in haystack:
                continue

            namespaced = f"mcp::{server_id}::{tool_name}"
            out.append(
                McpDiscoveredTool(
                    namespaced_tool_name=namespaced,
                    server_id=server_id,
                    tool_name=tool_name,
                    description=desc.strip(),
                    input_schema=t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {},
                    installed=(server_id, tool_name) in installed,
                    source="curated_server_cache",
                )
            )
            if len(out) >= limit:
                return out

    return out


def get_discovered_tool(
    *,
    server_id: str,
    tool_name: str,
) -> Optional[McpDiscoveredTool]:
    sid = (server_id or "").strip()
    tname = (tool_name or "").strip()
    if not sid or not tname:
        raise ValueError("server_id and tool_name are required.")
    require_trusted_mcp_server(sid, context="MCP discovery")

    installed = {(r.server_id, r.tool_name) for r in list_installed_records(enabled_only=True)}
    cache = load_mcp_tool_cache(sid)
    for t in cache.tools:
        if not isinstance(t, dict):
            continue
        if str(t.get("name") or "").strip() != tname:
            continue
        desc = str(t.get("description") or "")
        namespaced = f"mcp::{sid}::{tname}"
        return McpDiscoveredTool(
            namespaced_tool_name=namespaced,
            server_id=sid,
            tool_name=tname,
            description=desc.strip(),
            input_schema=t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {},
            installed=(sid, tname) in installed,
            source="curated_server_cache",
        )
    return None
