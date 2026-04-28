from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.assistant.utils.path_utils import get_repo_root


def get_mcp_install_registry_path() -> Path:
    return get_repo_root() / "mcp" / "installed_tools.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class McpInstallRecord:
    server_id: str
    tool_name: str
    namespaced_tool_name: str
    installed_at_utc: str
    install_source: str
    enabled: bool


def _default_registry_doc() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "installed_tools": [],
    }


def load_install_registry() -> dict[str, Any]:
    p = get_mcp_install_registry_path()
    if not p.exists():
        return _default_registry_doc()
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("MCP install registry must be a JSON object.")
    if raw.get("schema_version") != 1:
        raise ValueError(f"Unsupported MCP install registry schema_version: {raw.get('schema_version')!r}")
    tools = raw.get("installed_tools")
    if not isinstance(tools, list):
        raise ValueError("MCP install registry field 'installed_tools' must be a list.")
    return raw


def save_install_registry(doc: dict[str, Any]) -> Path:
    if not isinstance(doc, dict):
        raise ValueError("MCP install registry doc must be a dict.")
    if doc.get("schema_version") != 1:
        raise ValueError("MCP install registry schema_version must be 1.")
    if not isinstance(doc.get("installed_tools"), list):
        raise ValueError("MCP install registry requires 'installed_tools' list.")
    p = get_mcp_install_registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=True), encoding="utf-8")
    return p


def list_installed_records(*, enabled_only: bool = True) -> list[McpInstallRecord]:
    doc = load_install_registry()
    out: list[McpInstallRecord] = []
    for item in doc.get("installed_tools", []):
        if not isinstance(item, dict):
            continue
        server_id = item.get("server_id")
        tool_name = item.get("tool_name")
        namespaced = item.get("namespaced_tool_name")
        installed_at = item.get("installed_at_utc")
        install_source = item.get("install_source")
        enabled = bool(item.get("enabled", True))
        if enabled_only and not enabled:
            continue
        if not all(isinstance(v, str) and v.strip() for v in (server_id, tool_name, namespaced, installed_at, install_source)):
            continue
        out.append(
            McpInstallRecord(
                server_id=server_id.strip(),
                tool_name=tool_name.strip(),
                namespaced_tool_name=namespaced.strip(),
                installed_at_utc=installed_at.strip(),
                install_source=install_source.strip(),
                enabled=enabled,
            )
        )
    return out


def upsert_installed_tool(
    *,
    server_id: str,
    tool_name: str,
    namespaced_tool_name: str,
    install_source: str = "manual",
    enabled: bool = True,
) -> McpInstallRecord:
    if not isinstance(server_id, str) or not server_id.strip():
        raise ValueError("server_id must be a non-empty string.")
    if not isinstance(tool_name, str) or not tool_name.strip():
        raise ValueError("tool_name must be a non-empty string.")
    if not isinstance(namespaced_tool_name, str) or not namespaced_tool_name.strip():
        raise ValueError("namespaced_tool_name must be a non-empty string.")

    doc = load_install_registry()
    rows = doc.get("installed_tools")
    if not isinstance(rows, list):
        rows = []
        doc["installed_tools"] = rows

    key = (server_id.strip(), tool_name.strip())
    now = _utc_now_iso()
    updated = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (str(row.get("server_id") or "").strip(), str(row.get("tool_name") or "").strip()) != key:
            continue
        row["namespaced_tool_name"] = namespaced_tool_name.strip()
        row["installed_at_utc"] = now
        row["install_source"] = install_source
        row["enabled"] = bool(enabled)
        updated = True
        break

    if not updated:
        rows.append(
            {
                "server_id": server_id.strip(),
                "tool_name": tool_name.strip(),
                "namespaced_tool_name": namespaced_tool_name.strip(),
                "installed_at_utc": now,
                "install_source": install_source,
                "enabled": bool(enabled),
            }
        )

    save_install_registry(doc)
    return McpInstallRecord(
        server_id=server_id.strip(),
        tool_name=tool_name.strip(),
        namespaced_tool_name=namespaced_tool_name.strip(),
        installed_at_utc=now,
        install_source=install_source,
        enabled=bool(enabled),
    )
