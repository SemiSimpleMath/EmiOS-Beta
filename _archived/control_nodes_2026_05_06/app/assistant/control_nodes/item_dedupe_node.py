from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Set, Tuple

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc

logger = get_logger(__name__)

_DATETIME_KEYS: FrozenSet[str] = frozenset({
    "scheduled_start_utc",
    "scheduled_end_utc",
})

_TEXT_KEYS: FrozenSet[str] = frozenset({
    "summary",
    "calendar_status",
    "source_lifecycle_state",
    "email_subject",
    "email_sender",
    "email_summary",
})

_SOURCE_CONTENT_KEYS: FrozenSet[str] = _TEXT_KEYS | _DATETIME_KEYS

_FRIENDLY_FIELD_NAMES: Dict[str, str] = {
    "summary": "title",
    "scheduled_start_utc": "start time",
    "scheduled_end_utc": "end time",
    "calendar_status": "status",
    "source_lifecycle_state": "lifecycle state",
    "email_subject": "subject",
    "email_sender": "sender",
    "email_summary": "summary",
}


def _normalise_value(key: str, raw: Any) -> str:
    """Normalise a metadata value for comparison.

    Datetime fields are parsed to epoch seconds so that
    ``2026-03-20T16:00:00Z`` and ``2026-03-20T16:00:00+00:00``
    compare as equal.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    if key in _DATETIME_KEYS:
        parsed = parse_iso_utc(s)
        if parsed is not None:
            return str(int(parsed.timestamp()))
    return s


def _extract_content_fingerprint(meta: Dict[str, Any]) -> Dict[str, str]:
    """Return normalised source-content fields for comparison."""
    return {
        k: _normalise_value(k, meta.get(k))
        for k in _SOURCE_CONTENT_KEYS
        if meta.get(k) is not None
    }


class ItemDedupeNode(ControlNode):
    """
    Deterministic dedupe for normalized input items.

    1) Enforce intake contract: only ``state=new`` items enter the pipeline.
    2) Within-batch dedup by (item_id, event_type).
    3) If the item already exists in the dayflow DB, compare source-content
       fields (summary, schedule times, calendar status, email fields, etc.).
       - **Identical content** → drop.  The item is already known and nothing
         changed; sending it through triage wastes an LLM call.
       - **Changed content** → pass through with ``is_update=True`` and a list
         of ``update_changed_fields`` so triage can decide whether the update
         matters.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        from app.assistant.dayflow_orchestrator.contracts import validate_dayflow_items

        incoming = self.blackboard.get_state_value("intake_items_new", [])
        if incoming is None:
            incoming = []
        validate_dayflow_items(incoming, f"{self.name}::intake_items_new")
        if not isinstance(incoming, list):
            raise ValueError(f"[{self.name}] intake_items_new must be a list, got {type(incoming).__name__}.")

        existing_items = get_dayflow_items()

        existing_by_id: Dict[str, Dict[str, Any]] = {}
        for ex in existing_items:
            if not isinstance(ex, dict):
                continue
            ex_meta = get_meta(ex)
            ex_item_id = str(ex_meta.get("item_id") or ex.get("id") or "").strip()
            if ex_item_id:
                existing_by_id[ex_item_id] = ex_meta

        seen: Set[Tuple[str, str]] = set()
        deduped: List[Dict[str, Any]] = []
        dropped_batch_dupe = 0
        dropped_identical = 0
        passed_as_update = 0
        dropped_non_new_state = 0
        for idx, item in enumerate(incoming):
            if not isinstance(item, dict):
                raise ValueError(f"[{self.name}] intake_items_new[{idx}] must be an object.")
            meta = get_meta(item)
            state = str(meta.get("state") or "").strip().lower()
            if state != "new":
                dropped_non_new_state += 1
                continue
            item_id = str(meta.get("item_id") or "").strip()
            event_type = str(meta.get("event_type") or "").strip()
            key = (item_id, event_type)
            if item_id and key in seen:
                dropped_batch_dupe += 1
                continue
            if item_id:
                seen.add(key)

            if item_id and item_id in existing_by_id:
                db_meta = existing_by_id[item_id]
                db_state = str(db_meta.get("state") or "").strip().lower()
                # Items stuck at state=new were persisted before triage ran.
                # Let them through so triage can process them.
                if db_state == "new":
                    logger.info(
                        "[%s] item %s exists in DB with state=new (untriaged) — passing through for triage.",
                        self.name, item_id,
                    )
                    deduped.append(item)
                    continue
                incoming_fp = _extract_content_fingerprint(meta)
                db_fp = _extract_content_fingerprint(db_meta)
                changed = [
                    k for k in incoming_fp
                    if incoming_fp[k] != db_fp.get(k, "")
                ]
                if not changed:
                    dropped_identical += 1
                    continue
                friendly = [_FRIENDLY_FIELD_NAMES.get(k, k) for k in changed]
                meta["is_update"] = True
                meta["update_changed_fields"] = friendly
                passed_as_update += 1
                logger.info(
                    "[%s] item %s passed as UPDATE (changed: %s)",
                    self.name, item_id, ", ".join(friendly),
                )

            deduped.append(item)

        self.blackboard.update_state_value("intake_items_deduped", deduped)
        self.blackboard.update_state_value(
            "intake_dedupe_stats",
            {
                "input_count": len(incoming),
                "output_count": len(deduped),
                "dropped_batch_dupe": dropped_batch_dupe,
                "dropped_identical": dropped_identical,
                "passed_as_update": passed_as_update,
                "dropped_non_new_state": dropped_non_new_state,
                "existing_db_items_count": len(existing_by_id),
            },
        )
        logger.info(
            "[%s] deduped: input=%d output=%d dropped_non_new=%d dropped_batch=%d "
            "dropped_identical=%d passed_update=%d",
            self.name,
            len(incoming),
            len(deduped),
            dropped_non_new_state,
            dropped_batch_dupe,
            dropped_identical,
            passed_as_update,
        )
        self.blackboard.update_state_value("last_agent", self.name)
