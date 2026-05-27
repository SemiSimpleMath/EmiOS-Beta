"""
Daily Context Generator Step

Flow:
1. Load stage config, check gate conditions
2. Prepare inputs: Build context items (calendar, chat, tickets, AFK intervals)
3. Execute: Run daily_context_tracker agent
4. Output: Write resource_daily_context_generator_output.json

Notes:
- This is an agent-based stage (calls LLM) so it should be gated by AFK and interval.
- Output includes both LOCAL display strings and UTC ISO strings for downstream math.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.pipelines.dayflow.utils.context_sources import (
    get_calendar_events_upcoming_for_daily_context,
    get_calendar_events_completed,
    get_calendar_events_structured_for_day,
    get_current_presence_status,
    get_desktop_activity_recent,
    get_significant_activity_segments,
    get_responded_tickets_categorized,
)
from app.assistant.pipelines.dayflow.utils.expected_calendar_utils import (
    build_expected_calendar_markdown,
    normalize_expected_schedule_to_utc,
)
from app.assistant.pipelines.dayflow.utils.room_scope import resolve_room_scope
from app.assistant.pipelines.dayflow.utils.subconscious_projection import (
    project_subconscious_for_daily_context,
)
from app.assistant.utils.chat_formatting import messages_to_chat_excerpts
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context

logger = get_logger(__name__)


class DailyContextGeneratorStep(BaseStep):
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

    """
    Pipeline stage that runs the daily_context_tracker agent to generate
    daily context summary (what's happening today, user's state, etc.)
    """

    step_id: str = "daily_context_generator"

    # -------------------------------------------------------------------------
    # Config & Cursor
    # -------------------------------------------------------------------------

    def _output_filename(self) -> str:
        return "resource_daily_context_generator_output.json"

    def _expected_calendar_filename(self) -> str:
        return "resource_expected_calendar.json"

    def _expected_calendar_markdown_filename(self) -> str:
        return "resource_expected_calendar_markdown.md"

    def _build_open_day_expected_schedule(self, *, boundary_date: str) -> list[Dict[str, Any]]:
        return [
            {
                "title": "User's schedule is wide open",
                "start_local": "05:00 AM",
                "end_local": "12:00 AM",
                "status": "upcoming",
                "source": "fallback",
                "calendar_item_id": f"fallback_open_day_{boundary_date}",
                "start_utc": "",
                "end_utc": "",
            }
        ]

    def _build_expected_schedule_from_api_calendar(self, *, boundary_date: str) -> list[Dict[str, Any]]:
        calendar_events_structured = get_calendar_events_structured_for_day(str(boundary_date))
        seeded_expected_schedule: list[Dict[str, Any]] = []
        for ev in calendar_events_structured:
            if not isinstance(ev, dict):
                continue
            seeded_expected_schedule.append(
                {
                    "title": str(ev.get("title") or "Untitled"),
                    "start_local": str(ev.get("start_local") or ""),
                    "end_local": str(ev.get("end_local") or ""),
                    "status": str(ev.get("status") or "upcoming"),
                    "source": str(ev.get("source") or "google_calendar"),
                    "updated_at_local": str(ev.get("updated_at_local") or ""),
                    "calendar_item_id": str(ev.get("calendar_item_id") or ""),
                    "start_utc": str(ev.get("start_utc") or ""),
                    "end_utc": str(ev.get("end_utc") or ""),
                }
            )
        if seeded_expected_schedule:
            return seeded_expected_schedule
        return self._build_open_day_expected_schedule(boundary_date=str(boundary_date))

    def _build_expected_calendar_payload(
        self,
        *,
        boundary_date: str,
        now_local: datetime,
        now_utc: datetime,
        expected_schedule: list[Dict[str, Any]],
        source: str,
    ) -> Dict[str, Any]:
        normalized_schedule = normalize_expected_schedule_to_utc(
            expected_schedule,
            boundary_date_local=str(boundary_date),
        )
        return {
            "date": boundary_date,
            "expected_schedule": normalized_schedule,
            "source": source,
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "last_updated_utc": now_utc.isoformat(),
        }

    def _parse_iso_utc(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def _get_last_run_utc(self, ctx: StepContext) -> Optional[datetime]:
        """Get this step's last run timestamp from state."""
        step_runs = ctx.state.get("step_runs", {})
        step_info = step_runs.get(self.step_id, {}) or {}
        stage_info = step_info
        return self._parse_iso_utc(stage_info.get("last_run_utc"))

    def _get_stage_run_info(self, ctx: StepContext) -> Dict[str, Any]:
        step_runs = ctx.state.get("step_runs", {})
        info = step_runs.get(self.step_id, {}) if isinstance(step_runs, dict) else {}
        return info if isinstance(info, dict) else {}

    @staticmethod
    def _sha16(text: str) -> str:
        h = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
        return h[:16]

    @staticmethod
    def _parse_desktop_activity_local_timestamp(text: str) -> Optional[datetime]:
        """
        Best-effort parse for the desktop activity log format:
        starts with "YYYY-MM-DD HH:MM" (local time).
        """
        if not isinstance(text, str) or not text.strip():
            return None
        m = re.match(r"^\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\b", text)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        except Exception:
            return None

    def _gate_desktop_activity_recent(self, ctx: StepContext, text: str) -> Tuple[str, Dict[str, Any]]:
        """
        Only pass desktop activity if it's new since we last included it.
        We gate by:
        - content hash (always available)
        - and timestamp (if parsable) for extra safety
        """
        dbg: Dict[str, Any] = {}
        if not isinstance(text, str) or not text.strip():
            return "", {"desktop_activity_gate": "empty"}

        info = self._get_stage_run_info(ctx)
        seen_sha = info.get("desktop_activity_last_seen_sha")
        # Prefer the entry's own timestamp (minute granularity) if we captured it.
        seen_entry_ts_utc = self._parse_iso_utc(info.get("desktop_activity_last_seen_entry_ts_utc"))

        sha = self._sha16(text)
        dbg["desktop_activity_sha"] = sha
        if isinstance(seen_sha, str) and seen_sha == sha:
            return "", {"desktop_activity_gate": "same_sha"}

        # Parse the entry timestamp (local) if present.
        ts_local = self._parse_desktop_activity_local_timestamp(text)
        if ts_local is not None:
            # Attach local tzinfo (ctx.now_local already has it).
            try:
                if ctx.now_local.tzinfo is not None:
                    ts_local = ts_local.replace(tzinfo=ctx.now_local.tzinfo)
            except Exception:
                pass
            try:
                ts_utc = ts_local.astimezone(timezone.utc) if ts_local.tzinfo else None
            except Exception:
                ts_utc = None
            if ts_utc is not None:
                dbg["desktop_activity_ts_utc"] = ts_utc.isoformat()
                # If the entry timestamp is older than what we've already seen, skip it.
                # If it's equal (same minute), still allow it if the content hash changed.
                if seen_entry_ts_utc is not None and ts_utc < seen_entry_ts_utc:
                    return "", {"desktop_activity_gate": "older_ts"}
                dbg["desktop_activity_entry_ts_utc"] = ts_utc.isoformat()

        return text, {"desktop_activity_gate": "new"}

    def _get_afk_snapshot(self) -> Dict[str, Any]:
        """Best-effort realtime snapshot from AFKMonitor."""
        try:
            from app.assistant.ServiceLocator.service_locator import DI

            monitor = getattr(DI, "afk_monitor", None)
            if monitor is None:
                return {}
            snapshot = monitor.get_computer_activity()
            return snapshot if isinstance(snapshot, dict) else {}
        except Exception:
            return {}

    # -------------------------------------------------------------------------
    # Gate Logic
    # -------------------------------------------------------------------------

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        """Check if step should run based on interval and AFK guard."""
        step_cfg = ctx.step_config or {}
        run_policy = step_cfg.get("run_policy", {}) if isinstance(step_cfg, dict) else {}
        min_interval = int(run_policy.get("min_interval_seconds", 300))  # Default 5 min for agent stages

        last_run_utc = self._get_last_run_utc(ctx)
        if last_run_utc:
            elapsed = (ctx.now_utc - last_run_utc).total_seconds()
            if elapsed < min_interval:
                remaining = int(min_interval - elapsed)
                return False, f"interval={remaining}s remaining"

        afk_guard = step_cfg.get("afk_guard", {}) if isinstance(step_cfg, dict) else {}
        if isinstance(afk_guard, dict):
            snapshot = self._get_afk_snapshot()
            is_afk = bool(snapshot.get("is_afk", False))
            is_potentially_afk = bool(snapshot.get("is_potentially_afk", False))
            if afk_guard.get("skip_when_afk", True) and is_afk:
                return False, "afk_guard=afk"
            if afk_guard.get("skip_when_potentially_afk", False) and is_potentially_afk:
                return False, "afk_guard=potentially_afk"

        return True, "ready"

    # -------------------------------------------------------------------------
    # Context building helpers
    # -------------------------------------------------------------------------

    def _get_current_daily_context(self, ctx: StepContext) -> Dict[str, Any]:
        """Get current daily context from previous stage output."""
        try:
            existing = ctx.read_resource(self._output_filename())
            boundary_date = ctx.state.get("boundary_date_local") or ctx.now_local.strftime("%Y-%m-%d")
            if existing and existing.get("date") == boundary_date:
                return existing
            return {
                "expected_schedule": [],
                "day_theme": "",
                "milestones": [],
                "current_status": "",
                "calendar_events_structured": [],
            }
        except Exception:
            return {
                "expected_schedule": [],
                "day_theme": "",
                "milestones": [],
                "current_status": "",
                "calendar_events_structured": [],
            }

    def _get_daily_context_mode(self, current_context: Dict[str, Any], ctx: StepContext) -> str:
        """Determine if we should rebuild or incrementally update."""
        if not current_context.get("day_theme"):
            return "rebuild"
        boundary_date = ctx.state.get("boundary_date_local") or ctx.now_local.strftime("%Y-%m-%d")
        if current_context.get("date") != boundary_date:
            return "rebuild"
        return "incremental"

    @staticmethod
    def _blank_daily_context() -> Dict[str, Any]:
        return {
            "expected_schedule": [],
            "day_theme": "",
            "milestones": [],
            "current_status": "",
            "calendar_events_structured": [],
        }

    # -------------------------------------------------------------------------
    # Agent call
    # -------------------------------------------------------------------------

    def _call_agent(self, agent_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call the daily_context_tracker agent."""
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message

            agent = DI.agent_factory.create_agent("daily_context_tracker")
            scope_context = self._build_pipeline_scope_context(agent_input)
            result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope_context))

            if hasattr(result, "data") and isinstance(result.data, dict):
                return result.data
            elif isinstance(result, dict):
                return result
            else:
                return {"result": str(result)}

        except Exception as e:
            logger.error(f"Error calling daily_context_tracker agent: {e}")
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
        now_utc = ctx.now_utc
        now_local = ctx.now_local
        boundary_date = ctx.state.get("boundary_date_local") or now_local.strftime("%Y-%m-%d")
        fallback_expected_schedule = self._build_expected_schedule_from_api_calendar(boundary_date=str(boundary_date))

        expected_calendar_payload = ctx.read_resource(self._expected_calendar_filename())
        expected_calendar_schedule = (
            expected_calendar_payload.get("expected_schedule")
            if isinstance(expected_calendar_payload, dict)
            else None
        )
        expected_calendar_valid = (
            isinstance(expected_calendar_schedule, list)
            and len(expected_calendar_schedule) > 0
            and isinstance(expected_calendar_payload, dict)
            and expected_calendar_payload.get("date") == boundary_date
        )
        if not expected_calendar_valid:
            expected_calendar_schedule = fallback_expected_schedule
            expected_calendar_payload = self._build_expected_calendar_payload(
                boundary_date=str(boundary_date),
                now_local=now_local,
                now_utc=now_utc,
                expected_schedule=expected_calendar_schedule,
                source="api_seed_or_fallback",
            )
            expected_calendar_schedule = expected_calendar_payload.get("expected_schedule", [])
            ctx.write_resource(self._expected_calendar_filename(), expected_calendar_payload)

        # Step 1: Build context items that the agent needs
        stage_config = ctx.step_config or {}
        lookbacks = stage_config.get("lookback_hours", {}) if isinstance(stage_config, dict) else {}
        calendar_upcoming_hours = int(lookbacks.get("calendar_upcoming", 12))
        calendar_completed_hours = int(lookbacks.get("calendar_completed", 4))
        chat_excerpts_hours = int(lookbacks.get("chat_excerpts", 4))
        tickets_hours = int(lookbacks.get("recent_tickets", 2))

        current_daily_context = self._get_current_daily_context(ctx)
        current_daily_context["expected_schedule"] = expected_calendar_schedule
        daily_context_mode = self._get_daily_context_mode(current_daily_context, ctx)

        is_day_boundary_rebuild = False
        if daily_context_mode == "rebuild":
            prev_output = ctx.read_resource(self._output_filename())
            is_day_boundary_rebuild = (
                not isinstance(prev_output, dict)
                or prev_output.get("date") != boundary_date
                or prev_output.get("_reset_reason") == "daily_boundary"
            )
            if is_day_boundary_rebuild:
                # First post-boundary pass must be seeded only from today's calendar
                # (no continuity from prior daily context payloads).
                current_daily_context = self._blank_daily_context()

        # Get day_start for activity segments
        day_start_utc = self._parse_iso_utc(ctx.state.get("day_start_time_utc"))
        if not day_start_utc:
            day_start_utc = now_utc - timedelta(hours=8)

        activity_segments = get_significant_activity_segments(since_utc=day_start_utc)

        desktop_raw = get_desktop_activity_recent()
        desktop_activity_recent, desktop_dbg = self._gate_desktop_activity_recent(ctx, desktop_raw)

        if is_day_boundary_rebuild:
            chat_excerpts: list = []
            responded_tickets: dict = {}
            logger.info(
                "DailyContextGeneratorStep: day-boundary rebuild — suppressing chat excerpts and tickets "
                "to prevent yesterday's context from leaking into today's schedule."
            )
        else:
            chat_excerpts = self._get_recent_chat_excerpts(ctx, hours=chat_excerpts_hours)
            responded_tickets = get_responded_tickets_categorized(since_utc=now_utc - timedelta(hours=tickets_hours))

        context = {
            "history_scope": resolve_room_scope(pipeline_config=ctx.pipeline_config, step_config=ctx.step_config),
            "day_of_week": ctx.now_local.strftime("%A"),
            "daily_context_mode": daily_context_mode,
            "current_daily_context": current_daily_context,
            "expected_calendar_payload": expected_calendar_payload,
            "expected_calendar_markdown": build_expected_calendar_markdown(expected_calendar_schedule),
            "calendar_events_upcoming": get_calendar_events_upcoming_for_daily_context(hours=calendar_upcoming_hours),
            "recent_chat_excerpts": chat_excerpts,
            "calendar_events_completed": get_calendar_events_completed(hours=calendar_completed_hours),
            "recent_responded_tickets": responded_tickets,
            "presence_status": get_current_presence_status(),
            "activity_segments": activity_segments,
            "desktop_activity_recent": desktop_activity_recent,
        }

        # Step 2: Execute agent
        output = self._call_agent(context)

        if not output:
            logger.warning("DailyContextGeneratorStep: Agent returned no output")
            return StepResult(
                output={"error": "Agent returned no output"},
                debug={"mode": daily_context_mode},
            )

        # Step 3: Build and write output
        # Agent outputs: expected_schedule, day_theme, milestones, current_status
        milestones = output.get("milestones", [])
        if not isinstance(milestones, list):
            milestones = []

        expected_schedule = output.get("expected_schedule", [])
        if not isinstance(expected_schedule, list):
            expected_schedule = []
        if not expected_schedule:
            expected_schedule = expected_calendar_schedule
        expected_schedule = normalize_expected_schedule_to_utc(
            expected_schedule,
            boundary_date_local=str(boundary_date),
        )

        calendar_events_structured = get_calendar_events_structured_for_day(str(boundary_date))
        # Subconscious emergence: project active concerns + intentions +
        # latest arbiter plan into the same daily_context output so chat_gate
        # and dayflow's situation_audit pick them up via a single read.
        # Stratified by horizon so the 2-week-out birthday doesn't get
        # squeezed out by today's narrative synthesizer. Read-only; failures
        # degrade to empty fields instead of corrupting subconscious state.
        subconscious = project_subconscious_for_daily_context()
        stage_output: Dict[str, Any] = {
            "date": boundary_date,
            "expected_schedule": expected_schedule,
            "day_theme": output.get("day_theme", ""),
            "milestones": milestones,
            "current_status": output.get("current_status", ""),
            "calendar_events_structured": calendar_events_structured,
            "active_concerns_this_week": subconscious["active_concerns_this_week"],
            "active_concerns_longer_horizon": subconscious["active_concerns_longer_horizon"],
            "weekly_schedule_excerpt": subconscious["weekly_schedule_excerpt"],
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "last_updated_utc": now_utc.isoformat(),
        }

        ctx.write_resource(self._output_filename(), stage_output)
        ctx.write_resource(
            self._expected_calendar_filename(),
            self._build_expected_calendar_payload(
                boundary_date=str(boundary_date),
                now_local=now_local,
                now_utc=now_utc,
                expected_schedule=expected_schedule,
                source="daily_context",
            ),
        )
        expected_calendar_md = build_expected_calendar_markdown(expected_schedule)
        expected_calendar_md_path = ctx.resources_dir / self._expected_calendar_markdown_filename()
        expected_calendar_md_path.write_text(expected_calendar_md, encoding="utf-8")

        from app.assistant.utils.time_utils import get_local_timezone
        tz = get_local_timezone()
        calendar_resource: Dict[str, Any] = {
            "date": boundary_date,
            "timezone": str(getattr(tz, "key", tz)),
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "last_updated_utc": now_utc.isoformat(),
            "events": calendar_events_structured,
        }
        ctx.write_resource("resource_user_calendar.json", calendar_resource)

        # Mark desktop activity as "seen" if we actually included it.
        try:
            if isinstance(desktop_activity_recent, str) and desktop_activity_recent.strip():
                step_runs = ctx.state.setdefault("step_runs", {})
                info = step_runs.get(self.step_id)
                info_dict: Dict[str, Any] = info if isinstance(info, dict) else {}
                info_dict["desktop_activity_last_seen_sha"] = self._sha16(desktop_activity_recent)
                # Prefer entry timestamp (minute granularity) so we only re-include when
                # a newer desktop activity entry exists. Fall back to now_utc if parsing fails.
                try:
                    ts_local = self._parse_desktop_activity_local_timestamp(desktop_activity_recent)
                    if ts_local is not None and ctx.now_local.tzinfo is not None:
                        ts_local = ts_local.replace(tzinfo=ctx.now_local.tzinfo)
                    ts_utc = ts_local.astimezone(timezone.utc) if ts_local and ts_local.tzinfo else None
                except Exception:
                    ts_utc = None
                info_dict["desktop_activity_last_seen_entry_ts_utc"] = (ts_utc or ctx.now_utc).isoformat()
                step_runs[self.step_id] = info_dict
        except Exception:
            pass

        logger.info(
            f"DailyContextGeneratorStep: mode={daily_context_mode}, "
            f"milestones={len(milestones)}"
        )

        return StepResult(
            output=stage_output,
            debug={
                "mode": daily_context_mode,
                "milestone_count": len(milestones),
                "output_keys": list(output.keys())[:10],
                **(desktop_dbg if isinstance(desktop_dbg, dict) else {}),
            },
        )

    def _get_recent_chat_excerpts(self, ctx: StepContext, *, hours: int) -> list[dict]:
        """
        Read recent master_room chat directly from unified_log_2026.

        Was reading from DI.global_blackboard.get_recent_chat_since_utc,
        which is in-memory and doesn't survive Flask restart (and isn't
        reliably populated by the current chat ingest path anymore).
        unified_log_2026 is the canonical store; query it directly.

        Window: last 1 hour, master_room only. Chat-eligibility filter
        mirrors KG pipeline Stage 1 (chat sources, user/assistant role,
        non-empty message). Raw message text — no entity-resolution
        JOIN; daily context wants the user's actual words, not the
        resolver's substitution.

        ``hours`` and the room scope are accepted for signature
        compatibility but the query is hardcoded to master_room / 1h.
        """
        from types import SimpleNamespace
        from sqlalchemy import text as sql_text
        from app.models.db_manager import get_db_manager

        try:
            cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=1)
            # unified_log_2026.timestamp stores naive UTC as
            # "YYYY-MM-DD HH:MM:SS.ffffff" (space separator, no TZ marker).
            # ISO format with "T" + "+00:00" would not compare correctly.
            cutoff_str = cutoff_utc.strftime("%Y-%m-%d %H:%M:%S.%f")
            scope = resolve_room_scope(pipeline_config=ctx.pipeline_config, step_config=ctx.step_config)

            with get_db_manager().read_session() as session:
                rows = session.execute(
                    sql_text(
                        """
                        SELECT timestamp, role, speaker_name, room_id, message
                        FROM unified_log_2026
                        WHERE room_id = 'master_room'
                          AND source IN ('chat', 'room_slack', 'room_sms', 'room_ui')
                          AND role IN ('user', 'assistant')
                          AND message IS NOT NULL
                          AND TRIM(message) != ''
                          AND timestamp >= :cutoff
                        ORDER BY timestamp ASC
                        """
                    ),
                    {"cutoff": cutoff_str},
                ).fetchall()

            msgs = []
            for r in rows:
                ts_raw = r[0]
                if isinstance(ts_raw, str):
                    try:
                        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                else:
                    ts = ts_raw
                if ts and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                msgs.append(SimpleNamespace(
                    timestamp=ts,
                    role=r[1],
                    sender=r[2] or r[1] or "",
                    room_id=r[3],
                    content=r[4],
                    metadata=None,
                ))
            return messages_to_chat_excerpts(msgs, consumer_room_id=scope["room_id"])
        except Exception:
            logger.warning("DailyContextGeneratorStep: chat excerpt fetch failed", exc_info=True)
            return []

    def reset(self, ctx: StepContext) -> None:
        """Reset daily context at boundary using defaults from config."""
        now_utc = ctx.now_utc
        now_local = ctx.now_local

        stage_config = ctx.step_config or {}
        daily_reset = stage_config.get("daily_reset", {}) if isinstance(stage_config, dict) else {}
        defaults = daily_reset.get("output_resource_defaults", {}) if isinstance(daily_reset, dict) else {}

        boundary_date = ctx.state.get("boundary_date_local") or now_local.strftime("%Y-%m-%d")
        calendar_events_structured = get_calendar_events_structured_for_day(str(boundary_date))
        seeded_expected_schedule = self._build_expected_schedule_from_api_calendar(boundary_date=str(boundary_date))

        default_data: Dict[str, Any] = {
            "date": boundary_date,
            "expected_schedule": seeded_expected_schedule,
            "day_theme": defaults.get("day_theme", ""),
            "milestones": defaults.get("milestones", []),
            "current_status": defaults.get("current_status", ""),
            "calendar_events_structured": calendar_events_structured,
            # Subconscious fields are kept in the daily_boundary reset so the
            # schema stays consistent — concerns survive across day boundaries
            # and the projection re-reads the live register on the next run.
            "active_concerns_this_week": [],
            "active_concerns_longer_horizon": [],
            "weekly_schedule_excerpt": {},
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "last_updated_utc": now_utc.isoformat(),
            "_reset_reason": "daily_boundary",
        }

        ctx.write_resource(self._output_filename(), default_data)
        ctx.write_resource(
            self._expected_calendar_filename(),
            self._build_expected_calendar_payload(
                boundary_date=str(boundary_date),
                now_local=now_local,
                now_utc=now_utc,
                expected_schedule=seeded_expected_schedule,
                source="api_seed_or_fallback",
            ),
        )
        expected_calendar_md = build_expected_calendar_markdown(seeded_expected_schedule)
        expected_calendar_md_path = ctx.resources_dir / self._expected_calendar_markdown_filename()
        expected_calendar_md_path.write_text(expected_calendar_md, encoding="utf-8")
        logger.info("DailyContextGeneratorStep: daily reset complete")



