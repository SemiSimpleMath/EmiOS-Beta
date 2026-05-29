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


def _resolve_pod_allowed_scopes(tool_message: ToolMessage) -> List[str]:
    """Return the concrete pod scope_ids the caller can read.

    Reads ``scope_context.pods.allowed_scopes`` (default ``["self"]``) and
    expands the ``"self"`` token to the caller's room_id. Returns either:

    - ``["all"]`` — owner-wide; tool should not filter by scope_id
    - A list of explicit scope_ids — tool should restrict to those

    When scope_context is missing or has no room_id, falls back to
    unrestricted (``["all"]``) — this preserves behavior for system-
    internal callers (routines, dayflow ticks) that invoke pod tools
    without a room context.
    """
    scope_ctx = getattr(tool_message, "scope_context", None)
    if scope_ctx is None:
        return ["all"]
    # Defensive: scope_context might come through as dict on some paths.
    if isinstance(scope_ctx, dict):
        pods_block = scope_ctx.get("pods") or {}
        raw = pods_block.get("allowed_scopes") if isinstance(pods_block, dict) else None
        room_id = scope_ctx.get("room_id")
    else:
        pods_obj = getattr(scope_ctx, "pods", None)
        raw = getattr(pods_obj, "allowed_scopes", None) if pods_obj is not None else None
        room_id = getattr(scope_ctx, "room_id", None)
    if not isinstance(raw, list) or not raw:
        raw = ["self"]
    if "all" in raw:
        return ["all"]
    out: List[str] = []
    for s in raw:
        s_str = str(s).strip()
        if not s_str:
            continue
        if s_str == "self":
            if isinstance(room_id, str) and room_id.strip():
                expanded = room_id.strip()
                if expanded not in out:
                    out.append(expanded)
            # If self is requested but no room_id is set, skip silently —
            # the caller has no room identity to bind to.
            continue
        if s_str not in out:
            out.append(s_str)
    # If we ended up with nothing concrete, return [self]-shaped empty
    # (no readable scope) so the query filter genuinely returns nothing.
    # A sentinel "__none__" makes this unambiguous in the multi-scope path.
    return out or ["__none__"]


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
        linked_to_entity = (arguments.get("linked_to_entity") or "").strip() or None
        linked_via = arguments.get("linked_via") or None
        if linked_via is not None and not isinstance(linked_via, list):
            raise ValueError("`linked_via` must be a list of edge relationship_type strings")
        raw_limit = arguments.get("limit")
        limit = int(raw_limit) if raw_limit is not None else 20

        # `scope` is not an agent-facing argument — derived from the calling
        # scope_context.pods.allowed_scopes. Same pattern as send_email
        # reading scope_context.acting_as: the tool consumes the scope
        # dimension it needs as input.
        #
        # - allowed_scopes=["all"]: no filter (owner surfaces like master_room)
        # - allowed_scopes=["self"]: filter to this room's own pods (default)
        # - explicit room_ids: filter to that set (+ self if listed)
        allowed_scopes = _resolve_pod_allowed_scopes(tool_message)

        if allowed_scopes == ["all"]:
            # Owner-wide visibility — no per-scope filter on the query.
            pods = self._ensure_store().query(
                tags=tags, scope=None, kind=kind,
                linked_to_entity=linked_to_entity, linked_via=linked_via,
                since=since, query=query, limit=limit,
            )
        elif len(allowed_scopes) == 1:
            # Single-scope filter — passes straight to the existing query API.
            pods = self._ensure_store().query(
                tags=tags, scope=allowed_scopes[0], kind=kind,
                linked_to_entity=linked_to_entity, linked_via=linked_via,
                since=since, query=query, limit=limit,
            )
        else:
            # Multi-scope: query each scope and merge, deduped by pod_id.
            # Limit is applied per-scope, then trimmed in Python to honor
            # the caller's limit.
            seen: set[str] = set()
            merged: List = []
            for s in allowed_scopes:
                for pod in self._ensure_store().query(
                    tags=tags, scope=s, kind=kind,
                    linked_to_entity=linked_to_entity, linked_via=linked_via,
                    since=since, query=query, limit=limit,
                ):
                    if pod.pod_id in seen:
                        continue
                    seen.add(pod.pod_id)
                    merged.append(pod)
            pods = merged[:limit]
        headers = [_pod_to_header(p) for p in pods]
        summary_line = (
            f"Found {len(headers)} pod(s)"
            + (f" of kind={kind!r}" if kind else "")
            + (f" linked to {linked_to_entity!r}" if linked_to_entity else "")
            + (f" via {linked_via}" if linked_via else "")
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

        # Scope filter: pod_fetch returns only pods whose scope_id is in the
        # caller's allowed_scopes set. Mirrors pod_search; without it, a
        # planner that obtained a cross-room pod_id could fetch the body.
        # "all" → no filter. Otherwise restrict to the resolved scope list.
        allowed_scopes = _resolve_pod_allowed_scopes(tool_message)
        unrestricted = allowed_scopes == ["all"]
        allowed_set = set(allowed_scopes) if not unrestricted else set()

        store = self._ensure_store()
        fetched: List[Dict[str, Any]] = []
        missing: List[str] = []
        for pid in raw_ids:
            pod = store.get(str(pid))
            if pod is None:
                missing.append(str(pid))
                continue
            if not unrestricted and pod.scope_id not in allowed_set:
                # Cross-scope pod_id — not visible from this scope, treat as missing.
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
