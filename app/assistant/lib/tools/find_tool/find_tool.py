from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tool_registry.mcp_discovery import discover_mcp_tools
from app.assistant.utils.pydantic_classes import ScopeContext
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").strip().lower()) if t]


def _normalize_text(text: str) -> str:
    return " ".join(_tokenize(text))


def _score_match(query: str, tool_name: str, description: str) -> int:
    q = _normalize_text(query)
    n = _normalize_text(tool_name)
    d = _normalize_text(description)
    if not q:
        return 1
    if n == q:
        return 100
    if q in n:
        return 80
    tokens = _tokenize(q)
    if tokens and all(t in n for t in tokens):
        return 70
    if q in d:
        return 50
    if tokens and all(t in d for t in tokens):
        return 30
    if any(t in n for t in tokens):
        return 20
    if any(t in d for t in tokens):
        return 10
    return 0


def _semantic_overlap_score(query: str, tool_name: str, description: str) -> int:
    q_tokens = set(_tokenize(query))
    if not q_tokens:
        return 1
    n_tokens = set(_tokenize(tool_name))
    d_tokens = set(_tokenize(description))
    combined = n_tokens | d_tokens
    if not combined:
        return 0
    overlap = len(q_tokens & combined)
    if overlap <= 0:
        return 0
    coverage = overlap / max(len(q_tokens), 1)
    # Weighted boost for broader token coverage.
    return int(coverage * 60) + overlap


def _extract_scope_tool_policy(tool_message: ToolMessage) -> tuple[set[str] | None, set[str]]:
    """
    Returns (allowed_set_or_none_for_all, blocked_set).
    """
    scope = getattr(tool_message, "scope_context", None)
    if scope is None:
        return None, set()

    if isinstance(scope, ScopeContext):
        allowed_raw = scope.tools.allowed_tools
        blocked_raw = scope.tools.blocked_tools
    elif isinstance(scope, dict):
        tools = scope.get("tools")
        if not isinstance(tools, dict):
            raise ValueError("scope_context.tools must be a dict when scope_context is provided as dict.")
        allowed_raw = tools.get("allowed_tools", ["all"])
        blocked_raw = tools.get("blocked_tools", [])
    else:
        raise TypeError(f"Unsupported scope_context type: {type(scope)}")

    if not isinstance(allowed_raw, list):
        raise TypeError("scope_context.tools.allowed_tools must be a list.")
    if not isinstance(blocked_raw, list):
        raise TypeError("scope_context.tools.blocked_tools must be a list.")

    allowed_clean = {str(x).strip() for x in allowed_raw if isinstance(x, str) and str(x).strip()}
    blocked_clean = {str(x).strip() for x in blocked_raw if isinstance(x, str) and str(x).strip()}

    if "all" in allowed_clean and len(allowed_clean) > 1:
        raise ValueError("scope_context.tools.allowed_tools cannot mix 'all' with specific tool names.")

    allowed_set = None if ("all" in allowed_clean or not allowed_clean) else allowed_clean
    return allowed_set, blocked_clean


def _is_tool_visible_in_scope(tool_name: str, allowed_set: set[str] | None, blocked_set: set[str]) -> bool:
    if not isinstance(tool_name, str) or not tool_name.strip():
        return False
    name = tool_name.strip()
    if name in blocked_set:
        return False
    if allowed_set is None:
        return True
    return name in allowed_set


class FindToolTool(BaseTool):
    """
    Find candidate tools by name/description.
    """

    def __init__(self):
        super().__init__("find_tool")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data
        query = str(args.get("query") or "").strip()
        requested_limit = int(args.get("limit") or 8)
        if requested_limit <= 0:
            raise ValueError("limit must be a positive integer.")
        return_limit = min(requested_limit, 200)
        requested_tool_kind = str(args.get("tool_kind") or "").strip().lower()
        tool_kind = requested_tool_kind if requested_tool_kind else "all"
        if tool_kind == "mcp":
            tool_kind = "mcp_available"
        allowed_kinds = {"all", "local", "installed_mcp", "mcp_available"}
        if tool_kind not in allowed_kinds:
            raise ValueError(f"tool_kind must be one of {sorted(allowed_kinds)}, got: {tool_kind!r}")
        scope_allowed, scope_blocked = _extract_scope_tool_policy(tool_message)
        filtered_out_by_scope: set[str] = set()

        internal_limit = 200
        external_good_match_min_score = 30

        local_scored: List[Tuple[int, Dict[str, Any]]] = []
        external_scored: List[Tuple[int, Dict[str, Any]]] = []

        # Local/installed tools currently in ToolRegistry.
        if tool_kind in {"all", "local", "installed_mcp"}:
            tool_names = DI.tool_registry.list_tools() or []
            for name in tool_names:
                if not _is_tool_visible_in_scope(str(name), scope_allowed, scope_blocked):
                    filtered_out_by_scope.add(str(name))
                    continue
                is_mcp = str(name).startswith("mcp::")
                if tool_kind == "local" and is_mcp:
                    continue
                if tool_kind == "installed_mcp" and not is_mcp:
                    continue
                desc = DI.tool_registry.get_tool_description(name) or ""
                lexical_score = _score_match(query, str(name), str(desc))
                semantic_score = _semantic_overlap_score(query, str(name), str(desc))
                score = max(lexical_score, semantic_score)
                row = {
                    "tool_name": name,
                    "description": str(desc) if str(desc).strip() else "(no description provided)",
                }
                local_scored.append((score, row))

        # Trusted MCP cache discovery (including not-yet-installed tools).
        if tool_kind in {"all", "mcp_available"}:
            if not query:
                raise ValueError("query is required for find_tool.")
            query_terms: list[str] = [query]
            query_terms.extend(_tokenize(query))
            dedup_discovered: dict[str, Any] = {}
            for q in query_terms:
                for item in discover_mcp_tools(query=q, limit=internal_limit):
                    if not _is_tool_visible_in_scope(item.namespaced_tool_name, scope_allowed, scope_blocked):
                        filtered_out_by_scope.add(item.namespaced_tool_name)
                        continue
                    dedup_discovered[item.namespaced_tool_name] = item

            for item in dedup_discovered.values():
                lexical_score = _score_match(query, item.tool_name, item.description)
                semantic_score = _semantic_overlap_score(query, item.tool_name, item.description)
                score = max(lexical_score, semantic_score)
                if score <= 0:
                    continue
                # Favor exact MCP discoveries over fuzzy local matches.
                score += 40
                if score < external_good_match_min_score:
                    continue
                row = {
                    "tool_name": item.namespaced_tool_name,
                    "description": str(item.description).strip() if str(item.description).strip() else "(no description provided)",
                }
                external_scored.append((score, row))

        if filtered_out_by_scope:
            names = sorted(n for n in filtered_out_by_scope if isinstance(n, str) and n.strip())
            logger.debug(
                "[find_tool] Scope-filtered %d tool(s): %s",
                len(names),
                ", ".join(names),
            )

        # Deduplicate local and external sets independently.
        local_best_by_name: dict[str, tuple[int, Dict[str, Any]]] = {}
        for s, row in local_scored:
            name = str(row.get("tool_name") or "").strip()
            if not name:
                continue
            prev = local_best_by_name.get(name)
            if prev is None or s > prev[0]:
                local_best_by_name[name] = (s, row)
        local_deduped = list(local_best_by_name.values())
        local_deduped.sort(
            key=lambda item: (
                -int(item[0]),
                str(item[1].get("tool_name") or "").strip().lower(),
            )
        )

        external_best_by_name: dict[str, tuple[int, Dict[str, Any]]] = {}
        for s, row in external_scored:
            name = str(row.get("tool_name") or "").strip()
            if not name:
                continue
            prev = external_best_by_name.get(name)
            if prev is None or s > prev[0]:
                external_best_by_name[name] = (s, row)
        external_deduped = list(external_best_by_name.values())
        external_deduped.sort(
            key=lambda item: (
                -int(item[0]),
                str(item[1].get("tool_name") or "").strip().lower(),
            )
        )

        local_top = [row for _, row in local_deduped[:internal_limit]]
        external_good = [row for _, row in external_deduped]
        # External list excludes already-returned names from local list.
        local_names = {str(r.get("tool_name") or "").strip() for r in local_top}
        external_good = [r for r in external_good if str(r.get("tool_name") or "").strip() not in local_names]
        all_matches = local_top + external_good
        total_matches = len(all_matches)
        combined = all_matches[:return_limit]
        best = combined[0]["tool_name"] if combined else None

        if not combined:
            return ToolResult(
                result_type="tool_result",
                content=f"No tools matched query: {query}",
                data={
                    "query": query,
                    "tool_kind": tool_kind,
                    "matches": [],
                    "best_match": None,
                    "total_matches": 0,
                    "returned_matches": 0,
                    "truncated": False,
                },
            )

        preview_lines: list[str] = []
        for idx, row in enumerate(combined, start=1):
            tname = str(row.get("tool_name") or "").strip()
            desc = str(row.get("description") or "").strip()
            if not desc:
                desc = "(no description provided)"
            preview_lines.append(f"{idx}) {tname} - {desc}")

        preview_title = "Top matches (relevance-sorted):"
        content = preview_title + "\n" + "\n".join(preview_lines)

        return ToolResult(
            result_type="tool_result",
            content=content,
            data={
                "query": query,
                "tool_kind": tool_kind,
                "matches": combined,
                "best_match": best,
                "total_matches": total_matches,
                "returned_matches": len(combined),
                "truncated": total_matches > len(combined),
            },
        )


def get_tool_class():
    return FindToolTool
