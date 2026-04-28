from __future__ import annotations

from typing import Dict, List, Any
from datetime import datetime, timezone

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.state_store import get_dayflow_items
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

DAYFLOW_ROOM_ID = "dayflow_orchestrator"


class PlannerPersistNode(ControlNode):
    """
    Persists the planner's plan_synopses, planned_tasks, completed plans,
    and cancelled tasks to the DB immediately after the planner runs, then
    reloads active_plan_synopses AND active_dayflow_items onto the blackboard
    so downstream agents (state_mover, action_selector) read fresh data.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)

        self._persist_synopses(now_utc)
        self._persist_planned_tasks(now_utc)
        self._close_completed_plans(now_utc)
        self._cancel_tasks(now_utc)
        self._reload_active_plan_synopses()

        # Write planner watermark so RECENT CHANGES only shows what
        # happened between planner runs (not the planner's own output).
        from app.assistant.control_nodes.strategic_planner_prep_node import _write_watermark
        _write_watermark(now_utc)

        self.blackboard.update_state_value("last_agent", self.name)

    def _persist_synopses(self, now_utc: datetime) -> None:
        plan_synopses_raw = self.blackboard.get_state_value("plan_synopses", [])
        if not isinstance(plan_synopses_raw, list) or not plan_synopses_raw:
            return

        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch
        from app.assistant.dayflow_orchestrator.state_store import build_plan_synopsis_dicts

        try:
            dicts = build_plan_synopsis_dicts(plan_synopses_raw)
            if dicts:
                write_dayflow_items_batch(dicts, caller=f"{self.name}::synopses")
                logger.info(
                    "[%s] persisted %d plan synopsis(es) immediately after planner.",
                    self.name,
                    len(dicts),
                )
        except Exception as e:
            logger.error("[%s] failed persisting plan synopses: %s", self.name, e)
            logger.debug("[%s] synopsis persist exception details", self.name, exc_info=True)
            raise

    def _persist_planned_tasks(self, now_utc: datetime) -> None:
        planned_tasks_raw = self.blackboard.get_state_value("planned_tasks", [])
        if not isinstance(planned_tasks_raw, list) or not planned_tasks_raw:
            return

        from app.assistant.control_nodes.post_room_finalize_node import PostRoomFinalizeNode

        existing_meta_by_id: Dict[str, Dict[str, Any]] = {}
        existing_items = get_dayflow_items()
        for item in existing_items:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if item_id:
                existing_meta_by_id[item_id] = meta
            # Also key by short_id so planner can reference tasks by
            # the short numeric id it sees in prompts.
            short_id = str(meta.get("short_id") or "").strip()
            if short_id:
                existing_meta_by_id[short_id] = meta

        try:
            finalize_node = PostRoomFinalizeNode(
                name=f"{self.name}::task_builder",
                blackboard=self.blackboard,
                agent_registry=self.agent_registry,
                tool_registry=self.tool_registry,
            )
            messages = finalize_node._build_planned_task_messages(
                planned_tasks=planned_tasks_raw,
                existing_meta_by_id=existing_meta_by_id,
                now_utc=now_utc,
            )
            if messages:
                from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch
                # Convert Messages to dicts for write_dayflow_items_batch.
                batch_dicts = []
                for msg in messages:
                    meta = msg.metadata if isinstance(msg.metadata, dict) else {}
                    batch_dicts.append(meta)
                write_dayflow_items_batch(batch_dicts, caller=f"{self.name}::planned_tasks")
                # Clear planned_tasks so post_room_finalize_node does not
                # re-process and duplicate them.
                self.blackboard.update_state_value("planned_tasks", [])
                logger.info(
                    "[%s] persisted %d planned task(s) immediately after planner.",
                    self.name,
                    len(messages),
                )
        except Exception as e:
            logger.error("[%s] failed persisting planned tasks: %s", self.name, e)
            logger.debug("[%s] task persist exception details", self.name, exc_info=True)
            raise

    def _close_completed_plans(self, now_utc: datetime) -> None:
        completed_raw = self.blackboard.get_state_value("completed_plan_ids", [])
        if not isinstance(completed_raw, list) or not completed_raw:
            return

        completed_set = {str(x).strip() for x in completed_raw if str(x or "").strip()}
        if not completed_set:
            return

        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
        from app.assistant.dayflow_orchestrator.state_store import (
            load_existing_dayflow_items,
            RESOLVED_STATES,
        )

        try:
            all_items = load_existing_dayflow_items(include_terminal=True)
            _TERMINAL = RESOLVED_STATES
            closed_count = 0

            for item in all_items:
                if not isinstance(item, dict):
                    continue
                meta = get_meta(item)
                plan_id = str(meta.get("plan_id") or "").strip()
                if not plan_id or plan_id not in completed_set:
                    continue

                source_type = str(meta.get("source_type") or "").strip().lower()
                current_state = str(meta.get("state") or "").strip().lower()
                item_id = str(meta.get("item_id") or item.get("id") or "").strip()
                if not item_id:
                    continue

                if source_type == "plan_synopsis":
                    if current_state == "closed":
                        continue  # Already closed — don't re-stamp last_reviewed_at.
                    write_dayflow_item(
                        item_id,
                        updates={"plan_status": "completed", "completed_at": now_utc.isoformat()},
                        state="closed",
                        reason="plan_completed",
                        caller=f"{self.name}::close_completed_plans",
                    )
                    closed_count += 1
                elif source_type in ("plan_task", "plan") and current_state not in _TERMINAL:
                    write_dayflow_item(
                        item_id,
                        state="closed",
                        reason="plan_completed",
                        caller=f"{self.name}::close_completed_plans",
                    )
                    closed_count += 1

            if closed_count:
                logger.info(
                    "[%s] closed %d item(s) for completed plans: %s",
                    self.name,
                    closed_count,
                    completed_set,
                )
        except Exception as e:
            logger.error("[%s] failed closing completed plans: %s", self.name, e)
            logger.debug("[%s] close completed plans exception details", self.name, exc_info=True)
            raise

    def _cancel_tasks(self, now_utc: datetime) -> None:
        cancelled_raw = self.blackboard.get_state_value("closed_task_ids", [])
        if not isinstance(cancelled_raw, list) or not cancelled_raw:
            return

        cancelled_set = {str(x).strip() for x in cancelled_raw if str(x or "").strip()}
        if not cancelled_set:
            return

        from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_item
        from app.assistant.dayflow_orchestrator.state_store import (
            load_existing_dayflow_items,
            RESOLVED_STATES,
        )

        try:
            all_items = load_existing_dayflow_items(include_terminal=True)
            _TERMINAL = RESOLVED_STATES
            cancelled_count = 0

            for item in all_items:
                if not isinstance(item, dict):
                    continue
                meta = get_meta(item)
                item_id = str(meta.get("item_id") or item.get("id") or "").strip()
                short_id = str(meta.get("short_id") or "").strip()
                if not item_id:
                    continue
                if item_id not in cancelled_set and short_id not in cancelled_set:
                    continue
                current_state = str(meta.get("state") or "").strip().lower()
                if current_state in _TERMINAL:
                    continue

                write_dayflow_item(
                    item_id,
                    state="closed",
                    reason="cancelled_by_planner",
                    caller=f"{self.name}::cancel_tasks",
                )
                cancelled_count += 1

            if cancelled_count:
                logger.info(
                    "[%s] cancelled %d task(s): %s",
                    self.name,
                    cancelled_count,
                    cancelled_set,
                )
            else:
                # Tasks may already be closed by _close_completed_plans if the
                # planner both completed a plan and cancelled its tasks.
                logger.info(
                    "[%s] closed_task_ids %s — all already in terminal state (likely closed by completed_plan_ids).",
                    self.name,
                    cancelled_set,
                )
        except Exception as e:
            logger.error("[%s] failed cancelling tasks: %s", self.name, e)
            logger.debug("[%s] cancel tasks exception details", self.name, exc_info=True)
            raise

    def _reload_active_plan_synopses(self) -> None:
        from app.assistant.dayflow_orchestrator.state_store import load_existing_dayflow_items

        try:
            all_items = load_existing_dayflow_items(include_terminal=True)
            by_plan_id: Dict[str, Dict[str, Any]] = {}
            for item in all_items:
                if not isinstance(item, dict):
                    continue
                meta = get_meta(item)
                if str(meta.get("source_type") or "").strip().lower() != "plan_synopsis":
                    continue
                if str(meta.get("plan_status") or "").strip().lower() == "completed":
                    continue
                plan_id = str(meta.get("plan_id") or "").strip()
                if not plan_id:
                    continue
                by_plan_id[plan_id] = {
                    "plan_id": plan_id,
                    "objective": str(meta.get("objective") or "").strip(),
                    "synopsis": str(meta.get("synopsis") or "").strip(),
                    "success_criteria": str(meta.get("success_criteria") or "").strip(),
                    "step_outline": meta.get("step_outline") if isinstance(meta.get("step_outline"), list) else [],
                    "updated_at": str(meta.get("last_reviewed_at") or meta.get("created_at") or "").strip(),
                }
            rows = sorted(by_plan_id.values(), key=lambda r: r.get("updated_at", ""))
            self.blackboard.update_state_value("active_plan_synopses", rows[-25:])
            logger.info(
                "[%s] reloaded %d active_plan_synopses onto blackboard.",
                self.name,
                len(rows[-25:]),
            )
        except Exception as e:
            logger.error("[%s] failed reloading active_plan_synopses: %s", self.name, e)
            logger.debug("[%s] reload exception details", self.name, exc_info=True)
            raise
