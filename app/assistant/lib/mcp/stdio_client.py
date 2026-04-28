from __future__ import annotations

import json
import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Optional

from app.assistant.runtime import start_monitored_thread


@dataclass(frozen=True)
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

        self._reader = start_monitored_thread(
            owner="mcp_stdio_client",
            name="mcp-stdout-reader",
            target=self._read_stdout_loop,
            daemon=True,
            kind="io_reader",
            metadata={"component": "stdio_json_rpc_client", "stream": "stdout"},
        )
        self._stderr_reader = start_monitored_thread(
            owner="mcp_stdio_client",
            name="mcp-stderr-reader",
            target=self._read_stderr_loop,
            daemon=True,
            kind="io_reader",
            metadata={"component": "stdio_json_rpc_client", "stream": "stderr"},
        )

    def _read_stdout_loop(self) -> None:
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

    def _read_stderr_loop(self) -> None:
        if self.proc.stderr is None:
            return
        for line in self.proc.stderr:
            try:
                line = line.rstrip("\n")
            except Exception:
                pass
            if line:
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


def normalize_tools_list_response(resp: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Expected (per spec): {"result": {"tools":[...], "nextCursor": ...}}
    """
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

