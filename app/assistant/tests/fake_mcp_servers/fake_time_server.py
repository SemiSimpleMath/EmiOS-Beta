"""
Fake MCP stdio server for tests.

Implements:
- tools/list
- tools/call (get_current_time only)

This avoids external dependencies (no need to install mcp-server-time).
"""

from __future__ import annotations

import json
import sys


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


TOOLS = [
    {
        "name": "get_current_time",
        "description": "Get current time in a specific timezone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "IANA timezone name (e.g. 'UTC')"}
            },
            "required": ["timezone"],
            "additionalProperties": False,
        },
    },
    {
        "name": "convert_time",
        "description": "Convert time between timezones.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_timezone": {"type": "string"},
                "time": {"type": "string"},
                "target_timezone": {"type": "string"},
            },
            "required": ["source_timezone", "time", "target_timezone"],
            "additionalProperties": False,
        },
    },
]


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "fake-time", "version": "0.0.0"},
                    },
                }
            )
            continue

        if method == "tools/list":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": TOOLS},
                }
            )
            continue

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name != "get_current_time":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                            "isError": True,
                        },
                    }
                )
                continue
            tz = arguments.get("timezone", "UTC")
            # Stable deterministic output for testing
            payload = {
                "timezone": tz,
                "datetime": "2026-01-01T00:00:00+00:00",
                "is_dst": False,
            }
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(payload)}],
                        "isError": False,
                    },
                }
            )
            continue

        # Default: method not found
        _send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        )


if __name__ == "__main__":
    main()

