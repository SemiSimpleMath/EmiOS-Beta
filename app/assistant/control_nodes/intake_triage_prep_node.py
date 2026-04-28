"""Pre-LLM preparation node for intake_triage.

Loads dayflow items via ``get_dayflow_items()``, splits them into the
buckets that intake_triage needs, computes presentation values, and
writes the prepared data to the blackboard.

Replaces the combined work of pre_room_ingest_node, item_dedupe_node,
and cooldown_gate_node — which are no longer needed now that ingestion
persists items before the orchestrator runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.state_store import (
    RESOLVED_STATES,
    get_dayflow_items,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import get_local_timezone, parse_iso_utc

logger = get_logger(__name__)

_RECENTLY_COMPLETED_HOURS = 6


def _utc_to_relative(utc_str: str, now_utc: datetime) -> str:
    """Convert a UTC ISO string to a relative duration like '2h 15m ago'."""
    parsed = parse_iso_utc(utc_str)
    if parsed is None:
        return ""
    total_seconds = int((parsed - now_utc).total_seconds())
    if total_seconds < 0:
        h, m = divmod(abs(total_seconds) // 60, 60)
        return f"{h}h {m}m ago" if h else f"{m}m ago"
    h, m = divmod(total_seconds // 60, 60)
    return f"in {h}h {m}m" if h else f"in {m}m"


def _utc_to_local_str(utc_str: str) -> str:
    """Convert a UTC ISO string to a local time string."""
    parsed = parse_iso_utc(utc_str)
    if parsed is None:
        return ""
    local_tz = get_local_timezone()
    return parsed.astimezone(local_tz).strftime("%I:%M %p")


def _enrich_item_for_prompt(item: Dict[str, Any], now_utc: datetime) -> Dict[str, Any]:
    """Build a prompt-ready dict from a raw dayflow item.

    Adds computed presentation fields (local times, relative durations)
    to the item's metadata. Returns the original dict structure (with a
    ``metadata`` key) so downstream nodes that call ``get_meta(item)``
    continue to work.
    """
    meta = dict(get_meta(item))  # Shallow copy to avoid mutating the original.
    item_id = str(meta.get("item_id") or item.get("id") or "").strip()

    # Presentation fields computed at prep time.
    meta["created_ago"] = _utc_to_relative(str(meta.get("created_at") or ""), now_utc)
    if not meta.get("created_at_local"):
        meta["created_at_local"] = _utc_to_local_str(str(meta.get("created_at") or ""))

    return {"id": item.get("id", item_id), "metadata": meta}


class IntakeTriagePrepNode(ControlNode):
    """Pre-LLM node that prepares context for intake_triage.

    1. Loads all dayflow items via ``get_dayflow_items()``.
    2. Splits into: eligible (state=new), active context, plan synopses,
       recently completed.
    3. Filters out cooldown-blocked items.
    4. Enriches with presentation values.
    5. Writes prepared buckets to blackboard.
    6. Gates: skips intake_triage if nothing to triage.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)

        # Planning mode bypass — skip the full pipeline.
        room_mode = str(
            self.blackboard.get_state_value("room_mode", "") or ""
        ).strip().lower()
        if room_mode == "planning_mode":
            logger.info(
                "[%s] room_mode=planning_mode — routing to plan_mode.",
                self.name,
            )
            self.blackboard.update_state_value(
                "next_agent", "dayflow_orchestrator::plan_mode",
            )
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # 1. Load all items — single DB query.
        all_items = get_dayflow_items()


        # 2. Split into buckets.
        eligible: List[Dict[str, Any]] = []
        active_context: List[Dict[str, Any]] = []
        plan_synopses: List[Dict[str, Any]] = []
        recently_completed: List[Dict[str, Any]] = []

        completed_cutoff = now_utc.timestamp() - (_RECENTLY_COMPLETED_HOURS * 3600)

        for item in all_items:
            meta = get_meta(item)
            state = str(meta.get("state") or "").strip().lower()
            source_type = str(meta.get("source_type") or "").strip().lower()

            # Plan synopses are context, not triage candidates.
            if source_type == "plan_synopsis":
                plan_synopses.append(meta)
                continue

            # Chat items are context but not shown in triage or active items.
            if source_type == "chat":
                continue

            # Dispatch/result log entries are represented in the action log, not triage.
            if source_type in ("action_dispatch", "action_result", "action_log"):
                continue

            # New items are candidates for triage.
            if state == "new":
                # Check cooldown.
                raw_cooldown = meta.get("cooldown_until")
                if raw_cooldown:
                    cooldown_dt = parse_iso_utc(str(raw_cooldown))
                    if cooldown_dt is not None and now_utc < cooldown_dt:
                        continue
                eligible.append(_enrich_item_for_prompt(item, now_utc))
                continue

            # Resolved items that were recently reviewed → recently completed.
            if state in RESOLVED_STATES:
                reviewed_raw = str(meta.get("last_reviewed_at") or "").strip()
                reviewed_dt = parse_iso_utc(reviewed_raw)
                if reviewed_dt is not None and reviewed_dt.timestamp() >= completed_cutoff:
                    recently_completed.append(_enrich_item_for_prompt(item, now_utc))
                continue

            # Everything else is active context for duplicate checking.
            active_context.append(_enrich_item_for_prompt(item, now_utc))

        # 3. Write to blackboard.
        self.blackboard.update_state_value("eligible_items_now", eligible)
        self.blackboard.update_state_value("active_dayflow_items", active_context)
        self.blackboard.update_state_value("active_plan_synopses", plan_synopses)
        self.blackboard.update_state_value("recently_completed_items", recently_completed)

        logger.info(
            "[%s] prepared: eligible=%d active=%d synopses=%d recently_completed=%d",
            self.name,
            len(eligible),
            len(active_context),
            len(plan_synopses),
            len(recently_completed),
        )

        # 4. Gate — skip triage if nothing to process.
        if not eligible:
            logger.info(
                "[%s] no eligible items — skipping intake_triage.", self.name,
            )
            self.blackboard.update_state_value(
                "next_agent", "triage_spawn_guard_node",
            )

        self.blackboard.update_state_value("last_agent", self.name)
