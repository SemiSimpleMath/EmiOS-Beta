"""
Activity Tracker Step

Purpose:
- Process accepted tickets and update tracked activity counts
- Optionally call activity_tracker agent if new chat affects counts
- Handle AFK-based resets for activities configured with reset_on_afk
- Refresh resource_tracked_activities_output.json so minutes_since advances
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc, utc_to_local
from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.pipelines.dayflow.utils.room_scope import resolve_room_scope
from app.assistant.utils.chat_formatting import messages_to_chat_excerpts
from app.assistant.scope.loader import load_scope_for_source

logger = get_logger(__name__)


class ActivityTrackerStep(BaseStep):
    def _build_pipeline_scope_context(self, *, history_scope: Dict[str, Any] | None = None):
        scope_data = history_scope if isinstance(history_scope, dict) else {}
        room_id = str(scope_data.get("room_id") or "").strip()
        room_surface = str(scope_data.get("room_surface") or "").strip()
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=f"{self.step_id}_runner", identity_overrides={"room_id": room_id or None, "surface": (room_surface or None) or "pipeline"})

    step_id: str = "activity_tracker"

    def _output_filename(self) -> str:
        return "resource_tracked_activities_output.json"

    def _get_afk_snapshot(self, ctx: StepContext) -> Dict[str, Any]:
        """
        Get current AFK status from AFKMonitor.

        Strict contract: no fallbacks. If AFKMonitor is unavailable or returns
        an invalid payload, raise loudly so the pipeline fails fast.
        """
        from app.assistant.ServiceLocator.service_locator import DI

        monitor = getattr(DI, "afk_monitor", None)
        if monitor is None:
            logger.error("ActivityTrackerStage: DI.afk_monitor is missing (strict mode)")
            raise RuntimeError("AFKMonitor missing: DI.afk_monitor is None")

        snapshot = monitor.get_computer_activity()
        if not isinstance(snapshot, dict):
            logger.error("ActivityTrackerStage: AFKMonitor returned non-dict snapshot (strict mode)")
            raise RuntimeError(f"AFKMonitor returned invalid snapshot type: {type(snapshot)}")

        # Enforce presence of stable API keys. Values may be None, but keys must exist.
        if "last_afk_return_utc" not in snapshot:
            logger.error("ActivityTrackerStage: snapshot missing 'last_afk_return_utc' (strict mode)")
            raise RuntimeError("AFKMonitor snapshot missing required key: last_afk_return_utc")

        return snapshot

    def _get_last_run_utc(self, ctx: StepContext) -> Optional[datetime]:
        """Get this step's last run timestamp from state."""
        step_runs = ctx.state.get("step_runs", {})
        step_info = step_runs.get(self.step_id, {}) or {}
        stage_info = step_info  # legacy variable name in downstream code
        return parse_iso_utc(stage_info.get("last_run_utc"))

    def _get_last_afk_reset_utc(self, ctx: StepContext) -> Optional[datetime]:
        """Get timestamp of last AFK reset we performed."""
        step_runs = ctx.state.get("step_runs", {})
        step_info = step_runs.get(self.step_id, {}) or {}
        stage_info = step_info
        return parse_iso_utc(stage_info.get("last_afk_reset_utc"))

    def _set_last_afk_reset_utc(self, ctx: StepContext, dt: datetime) -> None:
        """Record when we last performed an AFK reset."""
        if "step_runs" not in ctx.state:
            ctx.state["step_runs"] = {}
        if self.step_id not in ctx.state["step_runs"]:
            ctx.state["step_runs"][self.step_id] = {}
        ctx.state["step_runs"][self.step_id]["last_afk_reset_utc"] = dt.isoformat()

    def _get_recent_chat_since(self, ctx: StepContext, cutoff_utc: datetime, limit: int | None = None) -> List[Dict[str, Any]]:
        """
        Activity-tracker policy:
        - use stage's last_run_utc as cutoff
        - default chat filters apply (no commands, no injections, no summaries)
        - return excerpt dicts for the activity_tracker agent
        """
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            scope = resolve_room_scope(pipeline_config=ctx.pipeline_config, step_config=ctx.step_config)

            msgs = DI.global_blackboard.get_recent_chat_since_utc(
                cutoff_utc,
                limit=limit,
                room_id=scope["room_id"],
                room_surface=scope["room_surface"],
                shared_room_ids=scope.get("shared_chat_room_ids", []),
            )
            return messages_to_chat_excerpts(msgs, consumer_room_id=scope["room_id"])
        except Exception:
            return []

    def _get_unprocessed_accepted_tickets(self) -> List[Any]:
        """
        Get ACCEPTED cron_reminder tickets that haven't had their status effects processed yet.
        Returns raw ticket objects so we can mark them as processed.
        """
        from app.assistant.ticket_manager import get_ticket_manager, TicketState

        try:
            return get_ticket_manager().get_tickets(
                ticket_type="cron_reminder",
                state=TicketState.ACCEPTED,
                effects_processed=0,
            )
        except Exception as e:
            logger.debug("Could not get accepted cron_reminder tickets: %s", e, exc_info=True)
            return []

    def _get_afk_intervals_since(self, since_utc: datetime, until_utc: datetime) -> List[Dict[str, Any]]:
        """Get AFK intervals that occurred between since_utc and until_utc."""
        try:
            from app.assistant.afk_manager.afk_statistics import get_afk_intervals_overlapping_range

            return get_afk_intervals_overlapping_range(since_utc, until_utc)
        except Exception as e:
            logger.warning(f"Could not get AFK intervals: {e}")
            return []

    def _get_activity_recorder(self, ctx: StepContext):
        """Get ActivityRecorder instance."""
        from app.assistant.pipelines.dayflow.activity.activity_recorder import ActivityRecorder

        # Cold start safety:
        # Seed from previous output so restart doesn't look like a reset.
        seed = ctx.read_resource(self._output_filename())
        seed_output = seed if isinstance(seed, dict) else None
        return ActivityRecorder(ctx.state, seed_output=seed_output)

    def _get_current_counts(self, recorder) -> Dict[str, int]:
        """Get current activity counts from recorder."""
        state = recorder.get_state()
        activities = state.get("activities", {})
        return {field_name: data.get("count_today", 0) for field_name, data in activities.items()}

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        """Check if step should run based on interval and AFK guard."""
        step_cfg = ctx.step_config or {}
        run_policy = step_cfg.get("run_policy", {}) if isinstance(step_cfg, dict) else {}
        min_interval = int(run_policy.get("min_interval_seconds", 60))

        last_run_utc = self._get_last_run_utc(ctx)
        if last_run_utc:
            elapsed = (ctx.now_utc - last_run_utc).total_seconds()
            if elapsed < min_interval:
                remaining = int(min_interval - elapsed)
                return False, f"interval={remaining}s remaining"

        afk_guard = step_cfg.get("afk_guard", {}) if isinstance(step_cfg, dict) else {}
        if isinstance(afk_guard, dict):
            snapshot = self._get_afk_snapshot(ctx)
            is_afk = bool(snapshot.get("is_afk", False))
            is_potentially_afk = bool(snapshot.get("is_potentially_afk", False))
            if afk_guard.get("skip_when_afk") and is_afk:
                return False, "afk_guard=afk"
            if afk_guard.get("skip_when_potentially_afk") and is_potentially_afk:
                return False, "afk_guard=potentially_afk"

        return True, "ready"

    def run(self, ctx: StepContext) -> StepResult:
        """
        Main run logic:
        1. Get accepted tickets since last run, update counts for tracked activities, mark as processed
        2. If new chat exists, call activity_tracker agent to detect count adjustments
        3. Check for AFK events and reset applicable timers
        4. Write output resource
        """
        now_utc = ctx.now_utc
        last_run_utc = self._get_last_run_utc(ctx)

        # Use a reasonable fallback if this is first run (e.g., 1 hour ago)
        if not last_run_utc:
            last_run_utc = now_utc - timedelta(hours=1)

        logger.debug(f"ActivityTrackerStep: running (last_run={last_run_utc.isoformat()})")

        recorder = self._get_activity_recorder(ctx)
        debug_info: Dict[str, Any] = {
            "last_run_utc": last_run_utc.isoformat(),
            "tickets_found": 0,
            "chat_messages": 0,
            "agent_called": False,
            "afk_reset_triggered": False,
        }

        # Step 1: Get accepted tickets since last run and update counts for tracked activities
        from app.assistant.ticket_manager import get_ticket_manager

        ticket_manager = get_ticket_manager()

        accepted_tickets = self._get_unprocessed_accepted_tickets()
        debug_info["tickets_found"] = len(accepted_tickets)

        for ticket in accepted_tickets:
            responded_at = ticket.responded_at or now_utc

            # status_effect is a list like ["finger_stretch", "coffee"]
            status_effect = ticket.status_effect or []
            if isinstance(status_effect, list):
                for activity_name in status_effect:
                    if activity_name in recorder._defs:
                        recorder.record_occurrence(activity_name, timestamp_utc=responded_at)
                        logger.info(f"ActivityTracker: recorded {activity_name} from ticket {ticket.ticket_id}")

            # Mark ticket as processed regardless of whether it's a tracked activity
            ticket_manager.mark_ticket_processed(ticket.id)

        # Step 2: Check for recent chat and optionally call activity_tracker agent
        recent_chat = self._get_recent_chat_since(ctx, last_run_utc)
        debug_info["chat_messages"] = len(recent_chat)

        if recent_chat:
            # Call activity_tracker agent to analyze chat for count adjustments
            try:
                from app.assistant.ServiceLocator.service_locator import DI
                from app.assistant.utils.pydantic_classes import Message

                agent = DI.agent_factory.create_agent("activity_tracker")
                current_counts = self._get_current_counts(recorder)

                agent_input: Dict[str, Any] = {
                    "current_activity_counts": current_counts,
                    "recent_chat": recent_chat,
                }
                history_scope = resolve_room_scope(pipeline_config=ctx.pipeline_config, step_config=ctx.step_config)
                scope_context = self._build_pipeline_scope_context(history_scope=history_scope)
                output = agent.action_handler(Message(agent_input=agent_input, scope_context=scope_context)).data
                debug_info["agent_called"] = True

                # Process agent output - it may provide updated counts and timer resets
                if isinstance(output, dict):
                    # Reset timers (sets last_occurrence_utc) for activities the agent detected
                    activities_to_reset = output.get("activities_to_reset") or []
                    if isinstance(activities_to_reset, list):
                        for field_name in activities_to_reset:
                            if field_name in recorder._defs:
                                recorder.record_occurrence(field_name, timestamp_utc=now_utc)
                                logger.info(f"ActivityTracker: timer reset for '{field_name}' from chat detection")

                    # Update counts for countable activities
                    activity_updates = output.get("activity_counts") or output.get("activities") or {}
                    for field_name, new_count in activity_updates.items():
                        if field_name in recorder._defs and isinstance(new_count, int):
                            current = current_counts.get(field_name, 0)
                            if new_count != current:
                                recorder.set_count_today(field_name, new_count)
                                logger.debug(f"ActivityTracker: agent adjusted {field_name}: {current} -> {new_count}")

            except Exception as e:
                logger.warning(f"ActivityTracker: agent call failed: {e}")

        # Step 3: Reset applicable timers when an AFK interval ended since last run.
        #
        # Primary source: DB-backed AFK intervals (restart-safe).
        # If any completed AFK interval ended between last_run and now, use the most
        # recent end_utc as the reset timestamp.
        # Fallback: in-memory last_afk_return_utc from the snapshot (covers the case
        # where the AFK interval DB hasn't been flushed yet for a very recent return).
        last_afk_reset_utc = self._get_last_afk_reset_utc(ctx)
        reset_dt = None
        reset_reason = None

        # Primary: query DB for AFK intervals that ended since last run.
        afk_intervals = self._get_afk_intervals_since(last_run_utc, now_utc)
        completed_returns = [
            parse_iso_utc(iv.get("end_utc"))
            for iv in afk_intervals
            if iv.get("end_utc") and iv.get("end_utc") != now_utc.isoformat()
        ]
        completed_returns = [dt for dt in completed_returns if dt is not None and dt <= now_utc]
        if completed_returns:
            reset_dt = max(completed_returns)
            reset_reason = "afk_interval_db"

        # Fallback: in-memory last_afk_return_utc from the monitor snapshot.
        if reset_dt is None:
            snapshot = self._get_afk_snapshot(ctx)
            last_return_iso = snapshot.get("last_afk_return_utc")
            dt_return = parse_iso_utc(last_return_iso) if isinstance(last_return_iso, str) else None
            if dt_return:
                reset_dt = dt_return
                reset_reason = "afk_return_utc_snapshot"

        if reset_dt and (last_afk_reset_utc is None or reset_dt > last_afk_reset_utc):
            recorder.reset_on_afk_return(timestamp_utc=reset_dt)
            self._set_last_afk_reset_utc(ctx, reset_dt)
            debug_info["afk_reset_triggered"] = True
            debug_info["afk_reset_at"] = reset_dt.isoformat()
            debug_info["afk_reset_reason"] = reset_reason
            logger.debug(f"ActivityTracker: AFK reset triggered ({reset_reason}) at {reset_dt.isoformat()}")

        # Step 4: Write output resource (this also updates minutes_since for all activities)
        payload = recorder.build_output_payload(now_utc=now_utc)
        ctx.write_resource(self._output_filename(), payload)

        output = {
            "last_run_utc": now_utc.isoformat(),
            "status": "ok",
        }

        return StepResult(
            output=output,
            debug=debug_info,
        )

    def reset(self, ctx: StepContext) -> None:
        """
        Reset at daily boundary (5 AM):
        - All tracked quantities set to 0
        - Clear last_afk_reset_utc so new day starts fresh
        """
        recorder = self._get_activity_recorder(ctx)

        day_start = ctx.state.get("day_start_time")
        if day_start:
            day_start_dt = parse_iso_utc(day_start)
            if day_start_dt:
                day_local_str = utc_to_local(day_start_dt).strftime("%Y-%m-%d")
            else:
                day_local_str = utc_to_local(ctx.now_utc).strftime("%Y-%m-%d")
        else:
            day_local_str = utc_to_local(ctx.now_utc).strftime("%Y-%m-%d")

        recorder.reset_for_new_day(day_local_str)
        payload = recorder.build_output_payload(now_utc=ctx.now_utc)
        ctx.write_resource(self._output_filename(), payload)

        if "step_runs" in ctx.state and self.step_id in ctx.state["step_runs"]:
            ctx.state["step_runs"][self.step_id].pop("last_afk_reset_utc", None)

        logger.info(f"ActivityTrackerStep: reset for new day {day_local_str}")



