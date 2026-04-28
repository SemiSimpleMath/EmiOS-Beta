from __future__ import annotations

import argparse
import json
import queue
import os
import shutil
import subprocess
import sys
import threading
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]

# Ensure repo root is importable so `import app...` works even when this file
# is executed as `python mcp/refresh_tool_cache.py` (sys.path[0] becomes `mcp/`).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # PyYAML is already in requirements.txt

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"Server entry must be a YAML mapping/object: {path}")
    return doc


def _find_server_entry_by_id(server_id: str) -> Path:
    servers_dir = REPO_ROOT / "mcp" / "servers"
    matches = []
    for p in servers_dir.rglob("*.y*ml"):
        if p.name.lower().endswith(".md"):
            continue
        try:
            doc = _load_yaml(p)
        except Exception:
            continue
        if doc.get("server_id") == server_id:
            matches.append(p)
    if not matches:
        raise FileNotFoundError(f"No MCP server entry found for server_id={server_id!r} under {servers_dir}")
    if len(matches) > 1:
        raise RuntimeError(f"Multiple MCP server entries found for server_id={server_id!r}: {matches}")
    return matches[0]


def _select_launch_option(server_entry: dict[str, Any], launch_id: Optional[str]) -> dict[str, Any]:
    opts = server_entry.get("launch_options") or []
    if not isinstance(opts, list) or not opts:
        raise ValueError("server entry must have non-empty launch_options")
    if launch_id:
        for opt in opts:
            if isinstance(opt, dict) and opt.get("id") == launch_id:
                return opt
        raise ValueError(f"launch option id not found: {launch_id!r}")
    # default: first option
    opt0 = opts[0]
    if not isinstance(opt0, dict):
        raise ValueError("launch_options[0] must be an object")
    return opt0


@dataclass
class JsonRpcResponse:
    raw: dict[str, Any]


class StdioJsonRpcClient:
    """
    Minimal line-delimited JSON-RPC client for MCP stdio servers.

    Many MCP servers accept one JSON object per line (stdin/stdout).
    """

    def __init__(self, proc: subprocess.Popen, *, timeout_s: float = 10.0):
        self.proc = proc
        self.timeout_s = timeout_s
        self._id = 0
        self._responses: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._stderr_lines: list[str] = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr_loop, daemon=True)
        self._stderr_reader.start()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id")
            if isinstance(msg_id, int):
                with self._lock:
                    q = self._responses.get(msg_id)
                if q is not None:
                    q.put(msg)
            # notifications are ignored for this refresh flow

    def _read_stderr_loop(self) -> None:
        if self.proc.stderr is None:
            return
        for line in self.proc.stderr:
            try:
                line = line.rstrip("\n")
            except Exception:
                pass
            if line:
                # keep a bounded buffer
                self._stderr_lines.append(line)
                if len(self._stderr_lines) > 200:
                    self._stderr_lines = self._stderr_lines[-200:]

    def stderr_preview(self, max_lines: int = 30) -> str:
        lines = self._stderr_lines[-max_lines:]
        return "\n".join(lines).strip()

    def request(self, method: str, params: Optional[dict[str, Any]] = None) -> JsonRpcResponse:
        self._id += 1
        req_id = self._id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params

        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._responses[req_id] = q

        if self.proc.poll() is not None:
            raise RuntimeError(
                f"MCP process exited before request '{method}'. "
                f"exit_code={self.proc.returncode}\n"
                f"stderr:\n{self.stderr_preview()}"
            )

        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(
                f"Failed to write request '{method}' to MCP process: {e}. "
                f"exit_code={self.proc.poll()}\n"
                f"stderr:\n{self.stderr_preview()}"
            ) from e

        try:
            msg = q.get(timeout=self.timeout_s)
        except Exception as e:
            raise TimeoutError(f"Timeout waiting for JSON-RPC response to {method} (id={req_id})") from e
        finally:
            with self._lock:
                self._responses.pop(req_id, None)

        return JsonRpcResponse(raw=msg)


def _normalize_tools_list_response(resp: dict[str, Any]) -> list[dict[str, Any]]:
    # Expected: {"result": {"tools":[...], "nextCursor": ...}} (per spec)
    result = resp.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        tools = result["tools"]
    elif isinstance(result, list):
        tools = result
    else:
        tools = []
    out = []
    for t in tools:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            out.append(t)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh MCP tool_cache from a stdio MCP server.")
    parser.add_argument("--server-id", required=True, help="e.g. io.modelcontextprotocol/time")
    parser.add_argument("--launch-id", default=None, help="launch_options[].id to use (e.g. uvx, pip, docker)")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout seconds")
    args = parser.parse_args(argv)

    entry_path = _find_server_entry_by_id(args.server_id)
    server_entry = _load_yaml(entry_path)
    launch = _select_launch_option(server_entry, args.launch_id)

    if launch.get("transport") != "stdio":
        raise ValueError("This refresher currently supports only transport=stdio")

    command = launch.get("command")
    cmd_args = launch.get("args")
    if not isinstance(command, str) or not isinstance(cmd_args, list):
        raise ValueError("launch option must include command (str) and args (list[str]) for stdio")

    # Resolve command for better cross-env behavior (PowerShell vs IDE).
    # - Prefer the current interpreter for python-based launch options.
    # - For other commands, require they exist on PATH (or be a path).
    resolved_command = command
    if command.strip() in {"python", "python3"}:
        resolved_command = sys.executable
    else:
        # If it's not a path, check PATH.
        if os.path.sep not in command and not (os.path.altsep and os.path.altsep in command):
            if shutil.which(command) is None:
                raise FileNotFoundError(
                    f"Cannot find MCP launch command on PATH: {command!r}\n"
                    f"- server_id: {args.server_id}\n"
                    f"- launch_id: {launch.get('id')!r}\n"
                    f"On Windows, verify with: `where {command}`\n"
                    f"If running from an IDE, ensure its Run Configuration inherits a PATH that includes this command."
                )

    # Build subprocess env. IMPORTANT: On Windows, CreateProcess expects a sane base
    # environment (e.g., SystemRoot, PATH). Starting from an empty env can trigger
    # WinError 87 "The parameter is incorrect".
    #
    # Also IMPORTANT: many IDEs inject PYTHONPATH to point at the repo, which can
    # shadow server dependencies (this repo contains a top-level `mcp/` pkg).
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if isinstance(launch.get("env"), dict):
        # Best-effort; caller should avoid secrets in repo configs.
        env.update({k: str(v) for k, v in launch["env"].items()})

    proc = subprocess.Popen(
        [resolved_command, *cmd_args],
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
        client = StdioJsonRpcClient(proc, timeout_s=args.timeout)

        # Some servers are strict MCP and require initialize; others (notably older time server)
        # may reject initialize. We attempt it, but proceed even if it errors.
        try:
            init = client.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "emiai-mcp-cache-refresher", "version": "0.1"},
                },
            ).raw
            if "error" in init:
                # ignore and continue
                pass
        except Exception:
            pass

        tools_resp = client.request("tools/list", {}).raw
        if "error" in tools_resp:
            raise RuntimeError(f"tools/list error: {tools_resp['error']}")

        tools = _normalize_tools_list_response(tools_resp)

        allowlist = server_entry.get("tool_allowlist") or []
        denylist = server_entry.get("tool_denylist") or []
        allowset = set(allowlist) if isinstance(allowlist, list) else set()
        denyset = set(denylist) if isinstance(denylist, list) else set()

        if allowset:
            tools = [t for t in tools if t.get("name") in allowset]
        if denyset:
            tools = [t for t in tools if t.get("name") not in denyset]

        # Write cache via the existing helper (keeps format stable)
        from app.assistant.lib.tool_registry.mcp_tool_cache import write_mcp_tool_cache

        retrieved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cache_path = write_mcp_tool_cache(args.server_id, tools=tools, retrieved_at=retrieved_at)
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

