from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


def get_mcp_tool_cache_dir() -> Path:
    return get_repo_root() / "mcp" / "tool_cache"


def sanitize_server_id_for_filename(server_id: str) -> str:
    # Stable, filesystem-safe key.
    return (
        server_id.replace("\\", "_")
        .replace("/", "__")
        .replace(":", "_")
        .replace(" ", "_")
    )


def cache_path_for_server(server_id: str) -> Path:
    return get_mcp_tool_cache_dir() / f"{sanitize_server_id_for_filename(server_id)}.json"


@dataclass(frozen=True)
class McpToolCacheLoadResult:
    server_id: str
    cache_path: Path
    tools: list[dict[str, Any]]
    errors: list[str]


def load_mcp_tool_cache(server_id: str) -> McpToolCacheLoadResult:
    """
    Load cached MCP `tools/list` payload for a given server.

    Cache format (schema_version=1):
      {
        "schema_version": 1,
        "server_id": "io.modelcontextprotocol/time",
        "retrieved_at": "ISO-8601 string (optional)",
        "tools": [
          {"name": "...", "description": "...", "inputSchema": {...}, "annotations": {...?}}
        ]
      }
    """
    p = cache_path_for_server(server_id)
    errors: list[str] = []
    tools: list[dict[str, Any]] = []

    if not p.exists():
        return McpToolCacheLoadResult(server_id=server_id, cache_path=p, tools=[], errors=[])

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        errors.append(f"Failed to parse JSON: {e}")
        return McpToolCacheLoadResult(server_id=server_id, cache_path=p, tools=[], errors=errors)

    if not isinstance(raw, dict):
        errors.append("Top-level cache JSON must be an object")
        return McpToolCacheLoadResult(server_id=server_id, cache_path=p, tools=[], errors=errors)

    schema_version = raw.get("schema_version")
    if schema_version != 1:
        errors.append(f"Unsupported schema_version: {schema_version!r} (expected 1)")

    sid = raw.get("server_id")
    if sid != server_id:
        errors.append(f"server_id mismatch: cache has {sid!r}, expected {server_id!r}")

    raw_tools = raw.get("tools", [])
    if not isinstance(raw_tools, list):
        errors.append("tools must be a list")
        raw_tools = []

    for i, t in enumerate(raw_tools):
        if not isinstance(t, dict):
            errors.append(f"tools[{i}] must be an object")
            continue
        name = t.get("name")
        input_schema = t.get("inputSchema")
        if not isinstance(name, str) or not name:
            errors.append(f"tools[{i}].name must be a non-empty string")
            continue
        if input_schema is not None and not isinstance(input_schema, dict):
            errors.append(f"tools[{i}].inputSchema must be an object if present")
            continue
        tools.append(t)

    return McpToolCacheLoadResult(server_id=server_id, cache_path=p, tools=tools, errors=errors)


def write_mcp_tool_cache(
    server_id: str,
    *,
    tools: list[dict[str, Any]],
    retrieved_at: Optional[str] = None,
) -> Path:
    """
    Write a cache file for a server.

    This is intentionally small and stable; do not include secrets.
    """
    cache_dir = get_mcp_tool_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_path_for_server(server_id)

    payload: dict[str, Any] = {
        "schema_version": 1,
        "server_id": server_id,
        "tools": tools,
    }
    if retrieved_at:
        payload["retrieved_at"] = retrieved_at

    p.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    logger.info(f"Wrote MCP tool cache: {p}")
    return p

