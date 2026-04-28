from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from app.assistant.lib.mcp.stdio_client import StdioJsonRpcClient, normalize_tools_list_response
from app.assistant.lib.tool_registry.mcp_tool_cache import write_mcp_tool_cache
from app.assistant.lib.tool_registry.mcp_trust_policy import require_trusted_mcp_server
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _select_launch_option(server_entry: dict[str, Any], launch_id: Optional[str]) -> dict[str, Any]:
    opts = server_entry.get("launch_options") or []
    if not isinstance(opts, list) or not opts:
        raise ValueError("server entry must include launch_options")

    if launch_id:
        for opt in opts:
            if isinstance(opt, dict) and str(opt.get("id")) == str(launch_id):
                if opt.get("transport") != "stdio":
                    raise ValueError(f"launch_id '{launch_id}' is not stdio transport.")
                return opt
        raise ValueError(f"launch_id '{launch_id}' not found for server entry.")

    for opt in opts:
        if isinstance(opt, dict) and opt.get("transport") == "stdio":
            return opt
    raise ValueError("No stdio launch option found for server entry.")


def _resolve_command(command: str) -> str:
    cmd = (command or "").strip()
    if not cmd:
        raise ValueError("launch option command is required.")
    if cmd in {"python", "python3"}:
        return sys.executable
    if os.path.sep in cmd or (os.path.altsep and os.path.altsep in cmd):
        if not os.path.exists(cmd):
            raise FileNotFoundError(f"Launch command path not found: {cmd}")
        return cmd
    found = shutil.which(cmd)
    if found is None:
        raise FileNotFoundError(f"Launch command not found on PATH: {cmd}")
    return found


def _resolve_env_placeholders(raw_env: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    for k, v in raw_env.items():
        key = str(k)
        val = str(v)
        matches = pattern.findall(val)
        if not matches:
            out[key] = val
            continue
        resolved = val
        for env_key in matches:
            env_val = os.environ.get(env_key)
            if env_val is None:
                raise ValueError(
                    f"Missing required environment variable '{env_key}' for MCP launch env key '{key}'."
                )
            resolved = resolved.replace(f"${{{env_key}}}", env_val)
        out[key] = resolved
    return out


def refresh_server_tool_cache(
    *,
    server_id: str,
    server_entry: dict[str, Any],
    launch_id: Optional[str] = None,
    timeout_s: float = 15.0,
) -> Path:
    if not isinstance(server_id, str) or not server_id.strip():
        raise ValueError("server_id must be a non-empty string.")
    if not isinstance(server_entry, dict):
        raise ValueError("server_entry must be a dict.")
    require_trusted_mcp_server(server_id, context="MCP cache refresh")

    launch = _select_launch_option(server_entry, launch_id)
    command = _resolve_command(str(launch.get("command") or ""))
    args = launch.get("args")
    if not isinstance(args, list):
        raise ValueError("launch option args must be a list.")
    args = [str(a) for a in args]

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    launch_env = launch.get("env")
    if isinstance(launch_env, dict):
        env.update(_resolve_env_placeholders(launch_env))

    proc = subprocess.Popen(
        [command, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(tempfile.gettempdir()),
    )

    try:
        client = StdioJsonRpcClient(proc, timeout_s=float(timeout_s))
        try:
            init_resp = client.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "emiai-mcp-installer", "version": "0.1"},
                },
            ).raw
            if isinstance(init_resp, dict) and "error" in init_resp:
                logger.debug("MCP initialize returned error (continuing): %s", init_resp.get("error"))
        except Exception as e:
            logger.debug("MCP initialize failed/skipped (continuing): %s", e)

        tools_resp = client.request("tools/list", {}).raw
        if "error" in tools_resp:
            raise RuntimeError(f"tools/list error: {tools_resp.get('error')}")
        tools = normalize_tools_list_response(tools_resp)

        allowlist = server_entry.get("tool_allowlist") or []
        denylist = server_entry.get("tool_denylist") or []
        allowset = set(allowlist) if isinstance(allowlist, list) else set()
        denyset = set(denylist) if isinstance(denylist, list) else set()
        if allowset:
            tools = [t for t in tools if t.get("name") in allowset]
        if denyset:
            tools = [t for t in tools if t.get("name") not in denyset]

        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cache_path = write_mcp_tool_cache(server_id, tools=tools, retrieved_at=retrieved_at)
        logger.info("Refreshed MCP tool cache for server %s (%d tools).", server_id, len(tools))
        return cache_path
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
