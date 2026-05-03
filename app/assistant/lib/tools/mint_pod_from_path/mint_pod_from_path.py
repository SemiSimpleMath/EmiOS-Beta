"""Mint a pod from a filesystem path.

Thin wrapper over `app.assistant.pod_store.file_ingest.ingest_file` so the
agent layer doesn't have to know about PodStore / image_ingest internals.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.pod_store.file_ingest import ingest_file
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class MintPodFromPathTool(BaseTool):
    def __init__(self):
        super().__init__("mint_pod_from_path")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else tool_data
        path = args.get("path") if isinstance(args, dict) else None

        if not isinstance(path, str) or not path.strip():
            return ToolResult(result_type="error", content="Missing required argument: path")

        expanded = os.path.expanduser(os.path.expandvars(path.strip()))
        src = Path(expanded).resolve()
        if not src.is_file():
            return ToolResult(
                result_type="mint_pod_from_path_error",
                content=f"File not found or not a regular file: {src}",
                data={"ok": False, "path": str(src)},
            )

        try:
            pod = ingest_file(src_path=src, source_kind="manual_mint")
        except Exception as e:
            logger.exception("[mint_pod_from_path] ingest failed for %s", src)
            return ToolResult(
                result_type="mint_pod_from_path_error",
                content=f"Failed to ingest {src.name}: {e}",
                data={"ok": False, "path": str(src), "error": str(e)},
            )

        md = pod.metadata or {}
        body_lines = [
            f"Minted pod for {src.name}",
            f"  pod_id: {pod.pod_id}",
            f"  kind:   {pod.kind}",
            f"  mime:   {md.get('mime_type') or 'image/' + (md.get('format') or '')}",
            f"  size:   {md.get('file_size_bytes', 0) // 1024}kb",
            f"  stored: {md.get('stored_path')}",
        ]
        return ToolResult(
            result_type="mint_pod_from_path_result",
            content="\n".join(body_lines),
            data={
                "ok": True,
                "pod_id": pod.pod_id,
                "kind": pod.kind,
                "mime_type": md.get("mime_type"),
                "sha256": md.get("sha256"),
                "stored_path": md.get("stored_path"),
                "file_size_bytes": md.get("file_size_bytes"),
            },
        )


def get_tool_class():
    return MintPodFromPathTool
