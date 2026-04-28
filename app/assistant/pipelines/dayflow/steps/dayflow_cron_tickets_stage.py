"""
Day Flow Cron Tickets Stage

Purpose:
- Own creation of small timer-based day-flow tickets (finger stretch, hydration, etc.).
- Produce and process cron-oriented candidates within this stage.
- Prevent duplicate ticket creation by processing each candidate batch only once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.assistant.pipelines.dayflow.utils.context_sources import (
    _format_ticket_for_context,
    build_time_since,
    get_calendar_events_for_orchestrator,
    get_responded_tickets_categorized,
)
from app.assistant.pipelines.dayflow.utils.room_scope import resolve_room_scope
from app.assistant.ticket_manager.ticket import TicketType
from app.assistant.utils.chat_formatting import messages_to_chat_excerpts
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc

logger = get_logger(__name__)


class DayFlowCronTicketsStep(BaseStep):
    def _build_pipeline_scope_context(self, agent_input: Dict[str, Any]):
        history_scope = agent_input.get("history_scope") if isinstance(agent_input, dict) else {}
        room_id = str(history_scope.get("room_id") or "").strip() if isinstance(history_scope, dict) else ""
        room_surface = str(history_scope.get("room_surface") or "").strip() if isinstance(history_scope, dict) else ""
        return build_pipeline_scope_context(
            pipeline_id="dayflow",
            actor_id=f"{self.step_id}_runner",
            room_id=room_id or None,
            room_surface=room_surface or None,
        )

    step_id: str = "dayflow_cron_tickets"

    def _source_filename(self) -> str:
        return "resource_dayflow_cron_candidates_output.json"

    def _output_filename(self) -> str:
        return "resource_dayflow_cron_tickets_output.json"

    def _cron_candidates_filename(self) -> str:
        return "resource_dayflow_cron_candidates_output.json"

    def _cron_suggestion_types(self) -> set[str]:
        """Derive cron suggestion types from the tracked activities config (single source of truth).

        Each user configures their own tracked activities, so this must read
        from the config file rather than a hardcoded list.
        """
        import json
        from app.assistant.pipelines.dayflow.activity.activity_recorder import TRACKED_CONFIG_FILE

        if not TRACKED_CONFIG_FILE.exists():
            raise RuntimeError(
                f"DayFlowCronTicketsStep: tracked activities config not found at {TRACKED_CONFIG_FILE}"
            )
        raw = json.loads(TRACKED_CONFIG_FILE.read_text(encoding="utf-8")) or {}
        activities = raw.get("activities", {}) if isinstance(raw, dict) else {}
        if not isinstance(activities, dict) or not activities:
            raise RuntimeError(
                "DayFlowCronTicketsStep: 'activities' is missing or empty in tracked activities config."
            )
        parsed = {str(key).strip() for key in activities if isinstance(key, str) and str(key).strip()}
        if not parsed:
            raise RuntimeError(
                "DayFlowCronTicketsStep: no valid activity keys found in tracked activities config."
            )
        return parsed

    def _split_suggestions_by_owner(
        self,
        *,
        suggestions: List[Dict[str, Any]],
        cron_types: set[str],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        cron: List[Dict[str, Any]] = []
        orchestration: List[Dict[str, Any]] = []
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            suggestion_type = str(suggestion.get("suggestion_type", "")).strip()
            if suggestion_type in cron_types:
                cron.append(suggestion)
            else:
                orchestration.append(suggestion)
        return orchestration, cron

    def _get_last_processed_source_utc(self, ctx: StepContext) -> Optional[datetime]:
        step_runs = ctx.state.get("step_runs", {})
        step_info = step_runs.get(self.step_id, {}) if isinstance(step_runs, dict) else {}
        return parse_iso_utc(step_info.get("last_processed_source_utc")) if isinstance(step_info, dict) else None

    def _set_last_processed_source_utc(self, ctx: StepContext, dt: datetime) -> None:
        step_runs = ctx.state.setdefault("step_runs", {})
        step_info = step_runs.get(self.step_id)
        info_dict: Dict[str, Any] = step_info if isinstance(step_info, dict) else {}
        info_dict["last_processed_source_utc"] = dt.isoformat()
        step_runs[self.step_id] = info_dict

    def _get_afk_snapshot(self) -> Dict[str, Any]:
        from app.assistant.ServiceLocator.service_locator import DI

        monitor = getattr(DI, "afk_monitor", None)
        if monitor is None:
            logger.error("DayFlowCronTicketsStep: DI.afk_monitor is missing")
            raise RuntimeError("AFKMonitor missing: DI.afk_monitor is None")
        snapshot = monitor.get_computer_activity()
        if not isinstance(snapshot, dict):
            logger.error("DayFlowCronTicketsStep: AFKMonitor returned invalid snapshot type=%s", type(snapshot))
            raise RuntimeError(f"AFKMonitor returned invalid snapshot type: {type(snapshot)}")
        return snapshot

    def _get_last_run_utc(self, ctx: StepContext) -> Optional[datetime]:
        """Get last run time for this stage."""
        step_runs = ctx.state.get("step_runs", {})
        last_run_str = step_runs.get(self.step_id, {}).get("last_run_utc")
        if last_run_str:
            try:
                dt = datetime.fromisoformat(last_run_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception as e:
                logger.warning(
                    f"DayFlowOrchestratorStep: could not parse last_run_utc '{last_run_str}': {e}",
                    exc_info=True,
                )
        return None

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        """Check if stage should run based on interval and AFK guard."""
        stage_cfg = ctx.step_config or {}
        run_policy = stage_cfg.get("run_policy", {}) if isinstance(stage_cfg, dict) else {}
        min_interval = int(run_policy.get("min_interval_seconds", 300))

        last_run_utc = self._get_last_run_utc(ctx)
        if last_run_utc:
            elapsed = (ctx.now_utc - last_run_utc).total_seconds()
            if elapsed < min_interval:
                remaining = int(min_interval - elapsed)
                return False, f"interval={remaining}s remaining"

        afk_guard = stage_cfg.get("afk_guard", {}) if isinstance(stage_cfg, dict) else {}
        if isinstance(afk_guard, dict):
            snapshot = self._get_afk_snapshot()
            is_afk = bool(snapshot.get("is_afk", False))
            is_potentially_afk = bool(snapshot.get("is_potentially_afk", False))
            if afk_guard.get("skip_when_afk", True) and is_afk:
                return False, "afk_guard=afk"
            if afk_guard.get("skip_when_potentially_afk", True) and is_potentially_afk:
                return False, "afk_guard=potentially_afk"

        return True, "ready"

    # -------------------------------------------------------------------------
    # Context building helpers
    # Note: Most data comes from resource files that agents subscribe to.
    # Only build context for data NOT available as resource files.
    # -------------------------------------------------------------------------

    def _get_location_summary(self) -> str:
        """Get current location summary."""
        try:
            from app.assistant.location_manager.location_manager import get_location_manager

            location_manager = get_location_manager()
            current = location_manager.get_current_location()
            return f"Currently: {current.get('label', 'Unknown')}"
        except Exception as e:
            logger.warning(f"Could not get location: {e}")
            return "Location unavailable"

    def _get_tracked_activity_names(self) -> set:
        """Get set of tracked activity field_names from config."""
        try:
            import json

            # Do NOT use __file__ relative paths here: this class may be relocated.
            # Source of truth is the legacy stage config folder.
            from app.assistant.pipelines.dayflow.activity.activity_recorder import TRACKED_CONFIG_FILE

            config_path = TRACKED_CONFIG_FILE
            if config_path.exists():
                config = json.loads(config_path.read_text(encoding="utf-8"))
                activities = config.get("activities", {})
                return {act.get("field_name") for act in activities.values() if act.get("field_name")}
            return set()
        except Exception as e:
            logger.warning(f"Could not load tracked activities config: {e}")
            return set()

    # Calendar/chat/tickets/time_since helpers live in utils.context_sources

    def _room_scope(self, ctx: StepContext) -> Dict[str, str]:
        return resolve_room_scope(pipeline_config=ctx.pipeline_config, step_config=ctx.step_config)

    def _get_active_tickets(self) -> List[Dict[str, Any]]:
        """Get cron reminder tickets that are genuinely pending user action.

        Excludes SNOOZED — those appear in recent_responded_tickets.snoozed
        with their snooze-until time.
        """
        try:
            from app.assistant.ticket_manager import get_ticket_manager, TicketState

            tickets = get_ticket_manager().get_tickets(
                states=[TicketState.PENDING, TicketState.PROPOSED],
                ticket_type=TicketType.CRON_REMINDER.value,
                limit=50,
            )
            return [_format_ticket_for_context(t) for t in tickets]
        except Exception as e:
            logger.warning(f"Could not get active tickets: {e}")
            return []

    def _get_recent_chat_messages(self, ctx: StepContext) -> Optional[List[Dict[str, Any]]]:
        scope = self._room_scope(ctx)
        cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=2)
        try:
            from app.assistant.ServiceLocator.service_locator import DI

            msgs = DI.global_blackboard.get_recent_chat_since_utc(
                cutoff_utc,
                limit=None,
                room_id=scope["room_id"],
                room_surface=scope["room_surface"],
                shared_room_ids=scope.get("shared_chat_room_ids", []),
            )
            return messages_to_chat_excerpts(msgs, consumer_room_id=scope["room_id"])
        except Exception as e:
            logger.error("DayFlowCronTicketsStep: failed loading scoped recent chat: %s", e)
            logger.debug("DayFlowCronTicketsStep scoped recent chat load exception details", exc_info=True)
            raise


    # -------------------------------------------------------------------------
    # Ticket creation
    # -------------------------------------------------------------------------

    def _create_tickets(
            self,
            suggestions: List[Dict[str, Any]],
            context: Dict[str, Any],
    ) -> int:
        """
        Create tickets from agent suggestions.

        Maps agent output fields to ticket_manager.create_ticket() parameters.
        Returns number of tickets created.
        """
        from app.assistant.ticket_manager import get_ticket_manager

        try:
            ticket_manager = get_ticket_manager()
        except Exception as e:
            logger.warning(f"Could not get ticket manager: {e}")
            return 0

        created = 0
        for suggestion in suggestions:
            suggestion_type = suggestion.get("suggestion_type", "general")
            title = suggestion.get("title", "")

            if not title:
                continue

            # Extract activity names from status_effects
            # Agent: [{"activity_name": "coffee"}, {"activity_name": "finger_stretch"}]
            # Ticket: ["coffee", "finger_stretch"]
            status_effects_list = suggestion.get("status_effects", [])
            status_effect = []
            if isinstance(status_effects_list, list):
                for item in status_effects_list:
                    if isinstance(item, dict) and item.get("activity_name"):
                        status_effect.append(item["activity_name"])

            try:
                ticket_room_id = str(context.get("history_scope", {}).get("room_id") or "").strip()
                if not ticket_room_id:
                    raise ValueError(
                        f"Cannot create ticket for suggestion '{title}': "
                        "context.history_scope.room_id is missing. "
                        "Dayflow pipeline must be seeded with a room_id in history_scope."
                    )
                ticket = ticket_manager.create_ticket(
                    ticket_type=TicketType.CRON_REMINDER,
                    suggestion_type=suggestion_type,
                    title=title,
                    message=suggestion.get("message", ""),
                    action_params={"priority": suggestion.get("priority", 5)},
                    trigger_context={
                        "room_id": ticket_room_id,
                        "location": context.get("location_summary", ""),
                        "time": context.get("day_of_week", ""),
                        "time_since": context.get("time_since", {}),
                    },
                    trigger_reason=suggestion.get("trigger_reason", ""),
                    valid_hours=4,
                    status_effect=status_effect,
                )
                if ticket:
                    # Propose and emit to UI
                    ticket_manager.mark_proposed(ticket.ticket_id)
                    self._emit_ticket_to_ui(ticket)
                    created += 1
                    logger.info(f"Created ticket: {suggestion_type} - {title}")
            except Exception as e:
                logger.warning(f"Error creating ticket: {e}")

        return created

    # -------------------------------------------------------------------------
    # UI emission
    # -------------------------------------------------------------------------

    def _emit_ticket_to_ui(self, ticket) -> None:
        """Emit a ticket to the frontend via WebSocket with TTS."""
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message, UserMessage, UserMessageData

            ticket_dict = ticket.to_dict() if hasattr(ticket, "to_dict") else ticket

            # Emit the suggestion data for the popup
            message = Message(event_topic="proactive_suggestion", data=ticket_dict)
            DI.event_hub.publish(message)

            # Also emit TTS message so Emi speaks the suggestion
            tts_text = ticket_dict.get("message") or ticket_dict.get("title", "I have a suggestion for you.")
            tts_message = UserMessage(
                data_type="user_msg",
                sender="dayflow_cron_tickets",
                receiver=None,
                timestamp=datetime.now(timezone.utc),
                role="assistant",
                user_message_data=UserMessageData(feed=None, tts=True, tts_text=tts_text),
            )
            tts_message.event_topic = "socket_emit"
            DI.event_hub.publish(tts_message)
            logger.debug(f"Emitted ticket to UI: {ticket_dict.get('ticket_id')}")

        except Exception as e:
            logger.warning(f"Could not emit ticket to UI: {e}")

    # -------------------------------------------------------------------------
    # Agent call
    # -------------------------------------------------------------------------

    def _call_agent(self, agent_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call the dayflow_cron_tickets agent."""
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message

            agent = DI.agent_factory.create_agent("dayflow_cron_tickets")
            scope_context = self._build_pipeline_scope_context(agent_input)
            result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope_context))

            if hasattr(result, "data") and isinstance(result.data, dict):
                return result.data
            elif isinstance(result, dict):
                return result
            else:
                return {"result": str(result)}

        except Exception as e:
            logger.error(f"Error calling dayflow_cron_tickets agent: {e}")
            return None

    # -------------------------------------------------------------------------
    # Main run
    # -------------------------------------------------------------------------

    def run(self, ctx: StepContext) -> StepResult:
        """
        1. Prepare inputs - build context items
        2. Execute agent
        3. Output
        """
        stage_config = ctx.step_config or {}
        lookbacks = stage_config.get("lookback_hours", {}) if isinstance(stage_config, dict) else {}
        tickets_hours = int(lookbacks.get("recent_tickets", 2))

        context = {
            "history_scope": self._room_scope(ctx),
            "day_of_week": ctx.now_local.strftime("%A"),
            "time_since": build_time_since(),
            "calendar_events": get_calendar_events_for_orchestrator(),
            "location_summary": self._get_location_summary(),
            "recent_chat_messages": self._get_recent_chat_messages(ctx),
            "active_tickets": self._get_active_tickets(),
            "recent_responded_tickets": get_responded_tickets_categorized(
                since_utc=ctx.now_utc - timedelta(hours=tickets_hours),
                ticket_type=TicketType.CRON_REMINDER.value,
            ),
        }

        # Step 2: Execute agent (context passed via agent_input)
        output = self._call_agent(context)

        if not output:
            logger.warning("DayFlowOrchestratorStep: Agent returned no output")
            return StepResult(
                output={"error": "Agent returned no output"},
                debug={},
            )

        # Step 3: Respect agent no-op signal.
        if output.get("no_action"):
            logger.info(
                "DayFlowCronTicketsStep: agent returned no_action=true, reason=%r",
                output.get("no_action_reason", ""),
            )
            ctx.write_resource(self._output_filename(), {
                **output,
                "suggestions": [],
                "last_updated": ctx.now_local.strftime("%Y-%m-%d %I:%M %p"),
                "tickets_created": 0,
                "cron_candidates_count": 0,
                "non_cron_candidates_count": 0,
            })
            return StepResult(
                output=output,
                debug={"no_action": True, "reason": output.get("no_action_reason", "")},
            )

        # Step 4: Split small timer/cron tickets from high-level orchestrator tickets.
        suggestions = output.get("suggestions", [])
        cron_types = self._cron_suggestion_types()
        orchestrator_suggestions: List[Dict[str, Any]] = []
        cron_suggestions: List[Dict[str, Any]] = []
        if isinstance(suggestions, list):
            orchestrator_suggestions, cron_suggestions = self._split_suggestions_by_owner(
                suggestions=suggestions,
                cron_types=cron_types,
            )

        # Step 5: Create tickets for cron suggestions owned by this stage.
        tickets_created = 0
        if cron_suggestions:
            tickets_created = self._create_tickets(cron_suggestions, context)

        # Persist candidate split for debugging/inspection.
        cron_payload: Dict[str, Any] = {
            "schema_version": 1,
            "source_step": self.step_id,
            "source_generated_at_utc": ctx.now_utc.isoformat(),
            "source_generated_at_local": ctx.now_local.strftime("%Y-%m-%d %I:%M %p"),
            "cron_suggestion_types": sorted(cron_types),
            "cron_suggestions": cron_suggestions,
            "non_cron_suggestions": orchestrator_suggestions,
            "context": {
                "day_of_week": context.get("day_of_week"),
                "location_summary": context.get("location_summary"),
                "time_since": context.get("time_since"),
            },
            "source_assessment": output.get("assessment", ""),
        }
        ctx.write_resource(self._cron_candidates_filename(), cron_payload)

        # Step 5: Build and write stage output (LOCAL time)
        now_local = ctx.now_local
        stage_output: Dict[str, Any] = {
            **output,
            "suggestions": cron_suggestions,
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "tickets_created": tickets_created,
            "cron_candidates_count": len(cron_suggestions),
            "non_cron_candidates_count": len(orchestrator_suggestions),
        }

        ctx.write_resource(self._output_filename(), stage_output)

        # Log summary
        suggestion_type = output.get("suggestion_type", "none")
        logger.info(
            f"DayFlowCronTicketsStep: suggestion_type={suggestion_type}, "
            f"cron_suggestions={len(cron_suggestions)}, "
            f"non_cron_suggestions={len(orchestrator_suggestions)}, "
            f"tickets_created={tickets_created}"
        )

        return StepResult(
            output=stage_output,
            debug={
                "output_keys": list(output.keys())[:10],
                "tickets_created": tickets_created,
            },
        )

