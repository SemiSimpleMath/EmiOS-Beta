"""Tool: refresh_wiki_page

Wraps `wiki_generator.growth.build_one_page` so a manager can refresh
one entity's wiki page after KG mutations changed the facts the page
rendered. Optional consistency critic run after writing.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _resolve_vault_path() -> Path:
    """Mirror of routes.wiki_viewer._wiki_vault_root() — read here to
    avoid pulling the Flask blueprint into tool execution."""
    override = os.environ.get("EMI_WIKI_DIR")
    if override:
        return Path(override)
    try:
        from app.assistant.utils.identity_names import get_assistant_name
        name = get_assistant_name() or "Emi"
    except Exception:
        name = "Emi"
    return Path.home() / f"{name}Wiki"


class RefreshWikiPageTool(BaseTool):
    def __init__(self):
        super().__init__("refresh_wiki_page")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data
        entity_label = (args.get("entity_label") or "").strip()
        run_critic = args.get("run_critic")
        if run_critic is None:
            run_critic = True
        reason = (args.get("reason") or "").strip()

        if not entity_label:
            return ToolResult(
                result_type="error",
                content="Missing required argument: entity_label",
            )

        vault_path = _resolve_vault_path()
        if not vault_path.exists():
            return ToolResult(
                result_type="error",
                content=f"Vault path does not exist: {vault_path}",
            )

        try:
            from app.assistant.wiki_generator.growth import build_one_page
        except Exception as e:
            return ToolResult(
                result_type="error",
                content=f"Failed to import wiki page builder: {e}",
            )

        logger.info(
            "[refresh_wiki_page] starting label=%s run_critic=%s reason=%s",
            entity_label, run_critic, reason or "(none)",
        )
        try:
            result = build_one_page(entity_label, vault_path, run_critic=bool(run_critic))
        except Exception as e:
            logger.error(
                "[refresh_wiki_page] failed for %s: %s",
                entity_label, e, exc_info=True,
            )
            return ToolResult(
                result_type="error",
                content=f"Refresh failed for {entity_label!r}: {e}",
                data={"ok": False, "entity_label": entity_label, "error": str(e)},
            )

        status = result.get("status")
        prose_path = result.get("prose")
        crit = result.get("critic_findings") if isinstance(result, dict) else None
        crit_count = (
            len(crit) if isinstance(crit, list)
            else int(crit.get("count", 0)) if isinstance(crit, dict)
            else 0
        )

        if status == "empty":
            return ToolResult(
                result_type="success",
                content=(
                    f"Entity {entity_label!r} has no prose page (not biographical "
                    f"enough — empty status)."
                ),
                data={
                    "ok": True,
                    "entity_label": entity_label,
                    "prose_path": None,
                    "status": "empty",
                    "critic_ran": False,
                    "critic_findings_count": 0,
                },
            )

        return ToolResult(
            result_type="success",
            content=(
                f"Refreshed wiki page for {entity_label!r}"
                + (f" — {crit_count} consistency-critic findings" if run_critic else "")
                + (f". Reason: {reason}" if reason else "")
            ),
            data={
                "ok": True,
                "entity_label": entity_label,
                "prose_path": str(prose_path) if prose_path else None,
                "status": status,
                "critic_ran": bool(run_critic),
                "critic_findings_count": crit_count,
            },
        )


def get_tool_class():
    return RefreshWikiPageTool
