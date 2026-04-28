"""
Refresh MCP tool cache for `pypi/mcp-github` (IDE-friendly).

Why this exists:
- IntelliJ/IDE run configs often do not inherit the same PATH as your shell.
- The generic `mcp/refresh_tool_cache.py` uses the server YAML launch options (uvx),
  so if `uvx` isn't discoverable in the IDE environment, it fails.

How to run (recommended in IntelliJ):
- Working directory: <repo root>
- Env vars:
  - GITHUB_TOKEN=... (required)
  - UVX_PATH=C:\\full\\path\\to\\uvx.exe (optional if uvx is on PATH)

Then run this file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def _repo_root_from_here() -> Path:
    # repo_root/mcp/refresh_github_cache_from_ide.py -> repo_root
    return Path(__file__).resolve().parents[1]


def _maybe_load_dotenv(repo_root: Path) -> None:
    """
    Minimal .env loader (no external dependency).
    Only sets keys that are not already present in os.environ.
    """
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # Best-effort; if parsing fails, we fall back to requiring explicit env vars.
        return


def _load_server_entry(server_id: str) -> dict:
    import yaml  # PyYAML is already in requirements.txt

    repo_root = _repo_root_from_here()
    servers_dir = repo_root / "mcp" / "servers"
    for p in servers_dir.rglob("*.y*ml"):
        if p.name.lower().endswith(".md"):
            continue
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(doc, dict) and doc.get("server_id") == server_id:
            return doc
    raise FileNotFoundError(f"Could not find MCP server entry for server_id={server_id!r} under {servers_dir}")


def _normalize_tools_list_response(resp: dict) -> list[dict]:
    result = resp.get("result", {})
    if isinstance(result, dict):
        tools = result.get("tools", [])
        if isinstance(tools, list):
            return [t for t in tools if isinstance(t, dict) and isinstance(t.get("name"), str)]
    return []


def main() -> int:
    server_id = "pypi/mcp-github"

    repo_root = _repo_root_from_here()
    _maybe_load_dotenv(repo_root)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "Missing GITHUB_TOKEN.\n"
            "Set it either:\n"
            "- as an environment variable in your IDE Run Configuration, or\n"
            "- in a repo-root `.env` file (gitignored) as: GITHUB_TOKEN=...\n"
        )

    entry = _load_server_entry(server_id)
    launch_opts = entry.get("launch_options") or []
    uvx_opt = None
    for opt in launch_opts:
        if isinstance(opt, dict) and opt.get("id") == "uvx":
            uvx_opt = opt
            break
    if not uvx_opt:
        raise RuntimeError("Server entry missing launch option id='uvx'.")

    # Resolve uvx executable (IDE-friendly).
    uvx_path = (os.environ.get("UVX_PATH") or "").strip()
    if uvx_path:
        if not Path(uvx_path).exists():
            raise FileNotFoundError(f"UVX_PATH points to missing file: {uvx_path}")
        uvx_cmd = uvx_path
    else:
        found = shutil.which("uvx")
        if not found:
            raise FileNotFoundError(
                "Cannot find 'uvx' on PATH.\n"
                "Fix options:\n"
                "- Set UVX_PATH to the full path of uvx.exe in your IDE env vars\n"
                "- Or update your IDE PATH to include the directory that contains uvx.exe\n"
            )
        uvx_cmd = "uvx"

    cmd = [uvx_cmd] + list(uvx_opt.get("args") or [])

    # Build environment: inherit base env (Windows needs SystemRoot/PATH), but remove
    # PYTHONPATH to avoid repo-local `mcp/` shadowing the PyPI `mcp` SDK in child processes.
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    # Launch from neutral cwd to avoid module shadowing.
    cwd = tempfile.gettempdir()

    # Lazy imports after sys.path is correct (repo root)
    import sys

    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)

    from app.assistant.lib.mcp.stdio_client import StdioJsonRpcClient
    from app.assistant.lib.tool_registry.mcp_tool_cache import write_mcp_tool_cache

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=cwd,
    )

    try:
        client = StdioJsonRpcClient(proc, timeout_s=float(entry.get("policy", {}).get("call_timeout_seconds", 30)))

        # Attempt initialize; ignore failures for older/quirky servers.
        try:
            client.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "emiai-mcp-cache-refresher", "version": "0.1"},
                },
            )
        except Exception:
            pass

        tools_resp = client.request("tools/list", {}).raw
        if "error" in tools_resp:
            raise RuntimeError(f"tools/list error: {tools_resp['error']}")

        tools = _normalize_tools_list_response(tools_resp)

        allowlist = entry.get("tool_allowlist") or []
        if isinstance(allowlist, list) and allowlist:
            allowset = set([t for t in allowlist if isinstance(t, str)])
            tools = [t for t in tools if t.get("name") in allowset]

        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cache_path = write_mcp_tool_cache(server_id, tools=tools, retrieved_at=retrieved_at)
        print(f"OK: wrote cache {cache_path} with {len(tools)} tool(s)")
        return 0
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


if __name__ == "__main__":
    raise SystemExit(main())

