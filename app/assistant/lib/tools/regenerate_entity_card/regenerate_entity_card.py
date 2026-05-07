"""Tool: regenerate_entity_card

Wraps `generate_and_persist_card(entity_label, force=True)` so a manager
can refresh one entity's card after KG mutations changed the facts the
card summarized. The shared card_gen_slot lock keeps concurrent
regenerations safe; this tool sets blocking=True so the call waits its
turn rather than failing fast.
"""
from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class RegenerateEntityCardTool(BaseTool):
    def __init__(self):
        super().__init__("regenerate_entity_card")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments", {}) if isinstance(tool_data.get("arguments"), dict) else tool_data
        entity_label = (args.get("entity_label") or "").strip()
        reason = (args.get("reason") or "").strip()

        if not entity_label:
            return ToolResult(
                result_type="error",
                content="Missing required argument: entity_label",
            )

        try:
            from app.assistant.pipelines.entity_cards.card_writer import (
                generate_and_persist_card,
            )
        except Exception as e:
            return ToolResult(
                result_type="error",
                content=f"Failed to import card writer: {e}",
            )

        logger.info(
            "[regenerate_entity_card] starting label=%s reason=%s",
            entity_label, reason or "(none)",
        )
        try:
            card = generate_and_persist_card(
                entity_label,
                force=True,
                blocking=True,
            )
        except Exception as e:
            logger.error(
                "[regenerate_entity_card] failed for %s: %s",
                entity_label, e, exc_info=True,
            )
            return ToolResult(
                result_type="error",
                content=f"Regenerate failed for {entity_label!r}: {e}",
                data={"ok": False, "entity_label": entity_label, "error": str(e)},
            )

        # The card dict varies in shape across versions; pull the
        # widely-stable fields and ignore the debug `_inputs`.
        summary = (card.get("summary") or "")[:200] if isinstance(card, dict) else ""
        key_facts = card.get("key_facts") if isinstance(card, dict) else None
        kf_count = len(key_facts) if isinstance(key_facts, list) else 0

        return ToolResult(
            result_type="success",
            content=(
                f"Regenerated entity card for {entity_label!r} "
                f"({kf_count} key facts). {('Reason: ' + reason) if reason else ''}"
            ).strip(),
            data={
                "ok": True,
                "entity_label": entity_label,
                "regenerated": True,
                "summary": summary,
                "key_facts_count": kf_count,
            },
        )


def get_tool_class():
    return RegenerateEntityCardTool
