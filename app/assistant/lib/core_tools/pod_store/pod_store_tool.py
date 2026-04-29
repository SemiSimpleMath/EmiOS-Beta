"""PodStoreTool — shared BaseTool implementation backing pod_search and pod_fetch.

Exposes two read-only tool names on a single class, mirroring the pattern used
by KnowledgeGraphSearch. Neither tool mutates pod_store.

- ``pod_search`` — returns a list of pod HEADERS (no body), for scan + decide.
- ``pod_fetch`` — returns full bodies for a list of pod_ids the caller chose.
"""
from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


def _pod_to_header(pod: Pod) -> Dict[str, Any]:
    """Cheap retrieval payload — everything an agent needs to decide whether to
    fetch the full body, with no body attached."""
    meta = pod.metadata or {}
    return {
        "pod_id": pod.pod_id,
        "kind": pod.kind,
        "tags": list(pod.tags),
        "one_liner": pod.one_liner,
        "scope_id": pod.scope_id,
        "created_by": pod.created_by,
        "created_at": pod.created_at.isoformat() if pod.created_at else None,
        "content_type": str(meta.get("critic_content_type") or ""),
    }


def _pod_to_full(pod: Pod) -> Dict[str, Any]:
    header = _pod_to_header(pod)
    header.update({
        "body": pod.body or "",
        "source_refs": [sr.model_dump() for sr in pod.source_refs],
        "for_agents": list(pod.for_agents),
        "metadata": dict(pod.metadata or {}),
    })
    return header


class PodStoreTool(BaseTool):
    """Read-only pod_store access exposed as tools."""

    def __init__(self) -> None:
        super().__init__("pod_store_tool")
        self._store: PodStore | None = None

    def _ensure_store(self) -> PodStore:
        if self._store is None:
            self._store = PodStore()
        return self._store

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            arguments = (tool_message.tool_data or {}).get("arguments", {}) or {}
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get("tool_name")
            if not tool_name:
                raise ValueError("Missing tool_name in tool_data.")
            handler = getattr(self, f"handle_{tool_name}", None)
            if handler is None:
                raise ValueError(f"Unsupported tool_name for PodStoreTool: {tool_name}")
            return handler(arguments, tool_message)
        except Exception as e:
            logger.error("PodStoreTool execute failed: %s", e)
            logger.debug("pod_store_tool execute exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="pod_store_tool_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )

    def publish_result(self, result: ToolResult) -> ToolResult:
        return result

    def publish_error(self, error_result: ToolResult) -> ToolResult:
        return error_result

    # ---------------------- HANDLERS ----------------------

    def handle_pod_search(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        tags = arguments.get("tags") or None
        if tags is not None and not isinstance(tags, list):
            raise ValueError("`tags` must be a list of strings")
        since = arguments.get("since") or None
        query = (arguments.get("query") or "").strip() or None
        kind = (arguments.get("kind") or "").strip() or None
        raw_limit = arguments.get("limit")
        limit = int(raw_limit) if raw_limit is not None else 20

        # `scope` is not an agent-facing argument. Cross-room retrieval is the
        # default (pods are user memory, not room-private). If a future policy
        # needs to narrow by the calling scope's room, derive it here from
        # tool_message.scope_context.room_id rather than asking the agent.
        scope = None

        pods = self._ensure_store().query(
            tags=tags,
            scope=scope,
            kind=kind,
            since=since,
            query=query,
            limit=limit,
        )
        headers = [_pod_to_header(p) for p in pods]
        summary_line = (
            f"Found {len(headers)} pod(s)"
            + (f" of kind={kind!r}" if kind else "")
            + (f" matching '{query}'" if query else "")
            + (f" tagged {tags}" if tags else "")
            + (f" since {since}" if since else "")
            + "."
        )
        return self.publish_result(
            ToolResult(
                result_type="pod_search",
                content=summary_line + "\n\n" + _format_headers_for_content(headers),
                data={"count": len(headers), "pods": headers},
            )
        )

    def handle_pod_fetch(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        raw_ids = arguments.get("pod_ids")
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("`pod_ids` must be a non-empty list of strings")

        store = self._ensure_store()
        fetched: List[Dict[str, Any]] = []
        missing: List[str] = []
        for pid in raw_ids:
            pod = store.get(str(pid))
            if pod is None:
                missing.append(str(pid))
                continue
            fetched.append(_pod_to_full(pod))

        data: Dict[str, Any] = {"pods": fetched, "missing": missing}
        content_parts = [f"Fetched {len(fetched)} pod(s); {len(missing)} missing."]
        for p in fetched:
            content_parts.append(
                f"\n--- {p['pod_id']} "
                f"[{', '.join(p['tags'])}]"
                f" · {p.get('content_type','')}"
                f" · scope={p.get('scope_id','')}"
                f" · {p.get('created_at','')} ---\n"
                f"one_liner: {p['one_liner']}\n"
                f"{p['body']}"
            )
        if missing:
            content_parts.append(f"\n\nMissing pod_ids: {missing}")
        return self.publish_result(
            ToolResult(
                result_type="pod_fetch",
                content="\n".join(content_parts),
                data=data,
            )
        )


def _format_headers_for_content(headers: List[Dict[str, Any]]) -> str:
    if not headers:
        return "(no pods matched)"
    lines = []
    for h in headers:
        lines.append(
            f"- {h['pod_id']} "
            f"[{', '.join(h['tags'])}] "
            f"· {h.get('content_type','')} "
            f"· scope={h.get('scope_id','')} "
            f"· {h.get('created_at','')}"
        )
        lines.append(f"    {h['one_liner']}")
    return "\n".join(lines)
