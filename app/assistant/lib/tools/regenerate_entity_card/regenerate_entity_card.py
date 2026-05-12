"""Tool: regenerate_entity_card

Wraps the entity_cards_v2 builder so a manager can refresh one entity's
card after KG mutations changed the facts the card summarized. Looks up
the kg_node by label, then calls ``build_card(entity_node_id)``.
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

        # Resolve label → kg_node id. Prefer category='person' when there are
        # multiple matches (most entity-card requests target people).
        from app.models.base import get_session
        from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

        session = get_session()
        try:
            nodes = (
                session.query(Node)
                .filter(Node.label == entity_label, Node.node_type == "Entity")
                .all()
            )
            if not nodes:
                return ToolResult(
                    result_type="error",
                    content=f"No KG node found with label {entity_label!r}",
                    data={"ok": False, "entity_label": entity_label},
                )
            node = next((n for n in nodes if n.category == "person"), nodes[0])
            entity_node_id = node.id
        finally:
            session.close()

        try:
            from app.assistant.pipelines.entity_cards_v2 import build_card
        except Exception as e:
            return ToolResult(
                result_type="error",
                content=f"Failed to import v2 card builder: {e}",
            )

        logger.info(
            "[regenerate_entity_card] starting label=%s node=%s reason=%s",
            entity_label, entity_node_id[:8], reason or "(none)",
        )
        try:
            result = build_card(entity_node_id)
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

        status = result.get("status") if isinstance(result, dict) else None
        sections = result.get("sections") if isinstance(result, dict) else None
        bullets = result.get("bullets") if isinstance(result, dict) else None

        if status != "ok":
            return ToolResult(
                result_type="success",
                content=f"Card for {entity_label!r}: {status}",
                data={"ok": True, "entity_label": entity_label, **(result or {})},
            )

        return ToolResult(
            result_type="success",
            content=(
                f"Regenerated v2 entity card for {entity_label!r} "
                f"({sections} sections, {bullets} bullets). "
                f"{('Reason: ' + reason) if reason else ''}"
            ).strip(),
            data={
                "ok": True,
                "entity_label": entity_label,
                "entity_node_id": entity_node_id,
                "regenerated": True,
                "sections": sections,
                "bullets": bullets,
            },
        )


def get_tool_class():
    return RegenerateEntityCardTool
