from __future__ import annotations

from typing import Iterable

# Trusted repos/sources: only MCP servers from these origins are discoverable and installable.
# Search and install flows restrict to this list.
TRUSTED_MCP_SOURCES: list[dict[str, str | list[str]]] = [
    {
        "name": "MCP official",
        "description": "Model Context Protocol reference servers",
        "repo_url": "https://github.com/modelcontextprotocol/servers",
        "server_ids": ["io.modelcontextprotocol/time"],
    },
    {
        "name": "GitHub",
        "description": "GitHub MCP (mcp-github, PyPI)",
        "repo_url": "https://github.com/modelContextProtocol/python-sdk",
        "server_ids": ["pypi/mcp-github"],
    },
    {
        "name": "Google",
        "description": "Google Maps MCP (official registry)",
        "repo_url": "https://www.npmjs.com/package/@modelcontextprotocol/server-google-maps",
        "server_ids": ["npm/server-google-maps"],
    },
    {
        "name": "Playwright",
        "description": "Playwright MCP (Microsoft)",
        "repo_url": "https://github.com/microsoft/playwright-mcp",
        "server_ids": ["npm/playwright-mcp"],
    },
]

# Derived allowlist: union of all server_ids from trusted sources.
# Single source of truth for "can we use this server" is this set.
TRUSTED_MCP_SERVER_IDS: frozenset[str] = frozenset(
    sid
    for src in TRUSTED_MCP_SOURCES
    for sid in (src.get("server_ids") or [])
    if isinstance(sid, str) and sid.strip()
)


def trusted_sources() -> list[dict[str, str | list[str]]]:
    """Return the list of trusted MCP repos/sources (GitHub, Google, Playwright, MCP official, etc.)."""
    return list(TRUSTED_MCP_SOURCES)


def trusted_server_ids() -> list[str]:
    return sorted(TRUSTED_MCP_SERVER_IDS)


def is_trusted_mcp_server(server_id: str) -> bool:
    return isinstance(server_id, str) and server_id.strip() in TRUSTED_MCP_SERVER_IDS


def require_trusted_mcp_server(server_id: str, *, context: str = "MCP operation") -> None:
    sid = (server_id or "").strip()
    if not sid:
        raise ValueError(f"{context}: server_id must be a non-empty string.")
    if not is_trusted_mcp_server(sid):
        allowed = ", ".join(trusted_server_ids())
        raise ValueError(
            f"{context}: server_id '{sid}' is not trusted. Trusted servers: [{allowed}]"
        )


def filter_to_trusted_servers(server_ids: Iterable[str]) -> list[str]:
    out: list[str] = []
    for sid in server_ids:
        if is_trusted_mcp_server(sid):
            out.append(sid.strip())
    return out
