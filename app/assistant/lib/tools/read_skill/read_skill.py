"""read_skill — load full SKILL.md bodies by name into the agent's context.

Companion to discover_skills. discover_skills returns headers; read_skill
returns the full markdown body — the actual recipe content that tells the
agent how to do the task. Accepting a list of names lets the planner load
multiple related skills in one turn (e.g. image-resize + pod-courier).

Missing names are reported in `missing: [...]` rather than raising; the
agent decides what to do with partial results.
"""
from typing import Any, Dict, List

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


_MAX_NAMES_PER_CALL = 5


class ReadSkillTool(BaseTool):
    """Load the full body of one or more skills by name."""

    requires_approval = False

    def __init__(self) -> None:
        super().__init__("read_skill")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments") or {}
        names_raw = args.get("names")

        # Accept either a single name string or a list of names. Normalize.
        if isinstance(names_raw, str):
            names = [names_raw.strip()] if names_raw.strip() else []
        elif isinstance(names_raw, list):
            names = [str(n).strip() for n in names_raw if isinstance(n, str) and str(n).strip()]
        else:
            return make_tool_error(
                error_code="invalid_arguments",
                message=(
                    "read_skill: `names` is required (string or list of strings). "
                    "Get candidate names from discover_skills."
                ),
                abort_policy="abort_tool",
                retryable=False,
            )

        if not names:
            return make_tool_error(
                error_code="invalid_arguments",
                message="read_skill: `names` must contain at least one non-empty name.",
                abort_policy="abort_tool",
                retryable=False,
            )

        # Dedup while preserving order.
        seen: set = set()
        deduped: List[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                deduped.append(n)
        names = deduped

        if len(names) > _MAX_NAMES_PER_CALL:
            return make_tool_error(
                error_code="too_many_names",
                message=(
                    f"read_skill: cap is {_MAX_NAMES_PER_CALL} skills per call (got {len(names)}). "
                    f"Bodies are large; fewer per turn keeps the context budget sane."
                ),
                abort_policy="abort_tool",
                retryable=False,
            )

        registry = DI.skill_registry
        found: List[Dict[str, Any]] = []
        missing: List[str] = []

        for name in names:
            skill = registry.get(name)
            if skill is None:
                missing.append(name)
                continue
            found.append({
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "body_chars": len(skill.body),
            })

        parts: List[str] = []
        for s in found:
            parts.append(f"=== skill: {s['name']} ===")
            parts.append(f"description: {s['description']}")
            parts.append("")
            parts.append(s["body"])
            parts.append("")
        if missing:
            parts.append("---")
            parts.append(f"Skills not found: {', '.join(missing)}")
            parts.append("Use discover_skills to find available names.")
        content = "\n".join(parts) if parts else "(no skills returned)"

        return ToolResult(
            result_type="tool_result",
            content=content,
            data={
                "found": found,
                "missing": missing,
                "found_count": len(found),
                "missing_count": len(missing),
            },
        )


def get_tool_class():
    return ReadSkillTool
