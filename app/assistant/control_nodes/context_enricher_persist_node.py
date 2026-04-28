"""
Context Enricher Persist Node

Takes the enricher planner's output and writes enrichment annotations
onto the admitted artifacts' metadata so downstream agents see them.

The planner returns results via return_control, so enrichments may be in:
- blackboard "result" (dict with "enrichments" key)
- blackboard "enrichments" (direct list, if set by a single-shot agent)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _get_meta(item: Dict[str, Any]) -> Dict[str, Any]:
    meta = item.get("metadata") if isinstance(item, dict) else {}
    return meta if isinstance(meta, dict) else {}


def _extract_enrichments(blackboard) -> List[Dict[str, Any]]:
    """Extract enrichments from wherever the planner left them."""
    # Try direct "enrichments" key first (single-shot agent path).
    enrichments = blackboard.get_state_value("enrichments", None)
    if isinstance(enrichments, list) and enrichments:
        return enrichments

    # Try "result" (planner return_control path).
    result = blackboard.get_state_value("result", None)
    if isinstance(result, dict):
        # Planner may return {"enrichments": [...]}
        enrichments = result.get("enrichments")
        if isinstance(enrichments, list):
            return enrichments
        # Or the result itself may be the action_input dict.
        action_input = result.get("action_input")
        if isinstance(action_input, dict):
            enrichments = action_input.get("enrichments")
            if isinstance(enrichments, list):
                return enrichments

    # Try parsing result as JSON string (some planners stringify).
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                enrichments = parsed.get("enrichments")
                if isinstance(enrichments, list):
                    return enrichments
        except (json.JSONDecodeError, TypeError):
            pass

    return []


class ContextEnricherPersistNode(ControlNode):
    """Write enrichment annotations onto admitted artifacts."""

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        admitted = self.blackboard.get_state_value("admitted_artifacts", []) or []
        if not isinstance(admitted, list):
            admitted = []

        enrichments_raw = _extract_enrichments(self.blackboard)

        if not enrichments_raw or not admitted:
            logger.info("[%s] nothing to persist (enrichments=%d, admitted=%d).",
                        self.name, len(enrichments_raw), len(admitted))
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Build lookup: item_id -> enrichment text.
        enrichment_map: Dict[str, str] = {}
        for entry in enrichments_raw:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("item_id") or "").strip()
            text = str(entry.get("enrichment") or "").strip()
            if item_id and text:
                enrichment_map[item_id] = text

        if not enrichment_map:
            logger.info("[%s] enricher returned no non-empty enrichments.", self.name)
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Stamp enrichment onto admitted artifacts in-memory.
        stamped = 0
        for item in admitted:
            meta = _get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if item_id in enrichment_map:
                meta["enrichment"] = enrichment_map[item_id]
                stamped += 1

        # Also persist to DB so the enrichment survives across cycles.
        if stamped > 0:
            try:
                from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
                for item_id, text in enrichment_map.items():
                    write_dayflow_item(item_id, updates={"enrichment": text}, caller=self.name)
            except Exception as e:
                logger.warning("[%s] failed to persist enrichments to DB: %s", self.name, e)

        self.blackboard.update_state_value("admitted_artifacts", admitted)
        self.blackboard.update_state_value("last_agent", self.name)

        logger.info("[%s] stamped %d enrichment(s) onto admitted artifacts.", self.name, stamped)
