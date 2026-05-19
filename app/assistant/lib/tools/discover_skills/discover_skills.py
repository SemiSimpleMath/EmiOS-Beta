"""discover_skills — browse / search the skill catalog by intent.

Skills (per agentskills.io) are SKILL.md files in `skills/<name>/`. The
SkillRegistry indexes them at boot. Today they only surface via auto-injection
(SkillInjector matches `auto_inject_when.task_keywords` against task +
incoming_message). If the planner's wording doesn't trip a keyword — e.g.
"shrink this photo" vs the `image-resize` skill's keyword "resize image" —
the skill stays invisible.

This tool gives agents an explicit handle: pass an intent query, get back a
ranked list of skill headers. Then call `read_skill` to load the full body of
the one(s) you want.

Pairs with `read_skill` the way `pod_search` pairs with `pod_fetch`.
"""
from typing import Any, Dict, List

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


_DEFAULT_LIMIT = 5
_MAX_LIMIT = 50


class DiscoverSkillsTool(BaseTool):
    """Search or browse the skill catalog. Returns headers; bodies via read_skill."""

    requires_approval = False

    def __init__(self) -> None:
        super().__init__("discover_skills")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments") or {}
        query = str(args.get("query") or "").strip()
        limit = self._resolve_limit(args.get("limit"))

        registry = DI.skill_registry

        if query:
            headers = registry.discover(query=query, limit=limit)
            total = len(headers)
            mode = "search"
        else:
            all_headers = registry.headers()
            all_headers.sort(key=lambda h: h.name)
            headers = all_headers[:limit]
            total = len(all_headers)
            mode = "browse"

        skills_payload: List[Dict[str, Any]] = []
        for h in headers:
            trigger = h.auto_inject_when
            auto_keywords = list(trigger.task_keywords) if trigger else []
            skills_payload.append({
                "name": h.name,
                "description": h.description,
                "auto_inject_keywords": auto_keywords,
            })

        if not skills_payload:
            content = (
                f"No skills matched query: {query!r}." if query
                else "No skills are registered. Check that skills/<name>/SKILL.md files exist."
            )
            return ToolResult(
                result_type="tool_result",
                content=content,
                data={
                    "query": query,
                    "mode": mode,
                    "count": 0,
                    "total_available": total,
                    "skills": [],
                },
            )

        lines: List[str] = []
        if mode == "search":
            lines.append(f"Skills matching {query!r}:")
        else:
            lines.append(f"Available skills (showing {len(skills_payload)} of {total}):")
        for idx, s in enumerate(skills_payload, start=1):
            lines.append(f"{idx}) {s['name']} — {s['description']}")
            if s["auto_inject_keywords"]:
                kw_preview = ", ".join(s["auto_inject_keywords"][:6])
                tail = "..." if len(s["auto_inject_keywords"]) > 6 else ""
                lines.append(f"   auto-injects on: {kw_preview}{tail}")
        lines.append("")
        lines.append("To read the full body of any of these, call read_skill with its name.")
        content = "\n".join(lines)

        return ToolResult(
            result_type="tool_result",
            content=content,
            data={
                "query": query,
                "mode": mode,
                "count": len(skills_payload),
                "total_available": total,
                "skills": skills_payload,
            },
        )

    @staticmethod
    def _resolve_limit(raw: Any) -> int:
        if raw is None or raw == "":
            return _DEFAULT_LIMIT
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return _DEFAULT_LIMIT
        if n <= 0:
            return _DEFAULT_LIMIT
        return min(n, _MAX_LIMIT)


def get_tool_class():
    return DiscoverSkillsTool
