"""
Health Status Stage

Generates resource_user_health_status.md — a compact, belief-derived health context
document covering chronic conditions, current sleep state, and active temporary issues.

Runs once per boundary day (not hourly — health facts are stable day-to-day).

Writes:
  - resources/dayflow_pipeline_outputs/resource_user_health_status.md  (latest, injected)
  - day_context/YYYY/MM/YYYY-MM-DD/resource_user_health_status.md      (archive)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.atomic_write import write_text_atomic
from app.assistant.utils.logging_config import get_logger
from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.path_utils import get_resources_dir as _get_resources_dir

logger = get_logger(__name__)

_LATEST_FILENAME = "resource_user_health_status.md"
_POINTER_FILENAME = "resource_user_health_status_latest.json"
_BELIEFS_PATH = _get_resources_dir() / "kg_derived" / "resource_user_beliefs.json"
_WEEKLY_INSIGHTS_PATH = (
    _get_resources_dir() / "weekly_insights_pipeline_outputs" / "resource_weekly_insights_latest.json"
)

# Belief domains to include in the health status document
_HEALTH_DOMAINS = {"health", "sleep"}

# Belief keys that are NOT physical/medical — they belong in routine or other contexts.
# These are excluded even if they appear in a health/sleep domain.
_EXCLUDED_BELIEF_KEYS = {
    "health.seyfarth_work_cutoff_1700_no_pings",
    "health.fri_dog_walk_after_friday_night_meats_zoom",
    "health.evening_family_time_1800_protected",
    "health.pet_emergency_jobs_require_explicit_consent",
    "health.monthly_timesheet_day_first_business_day",
    "health.fri_friday_night_meats_decompression_policy",
    "health.wellness_preference_proactive_gentle_nudges_all_day",
    "health.wellness_avoid_skydiving_extreme_heights",
    "health.wellness_digital_wellbeing_podcasts_over_doomscrolling",
    "health.lunch_break_prefer_emi_coding_at_keyboard",
    "health.nutrition_grocery_staples_ralphs_household",
    "health.nutrition_grocery_snacks_treats_resist_junk",
    "health.nutrition_takeout_irvine_breakfast_poached_avoid_snooze",
    "health.nutrition_snack_preference_triple_cream_brie_water_crackers",
    "health.dog_yard_access_free_suppress_yard_break_reminders",
    "health.coffee_preference_sweet_creamy_creamer_room_temp",
}

_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _format_health_beliefs() -> str:
    if not _BELIEFS_PATH.exists():
        logger.error(
            "[HealthStatusStage] Beliefs file not found at %s — health status will have no belief context.",
            _BELIEFS_PATH,
        )
        return ""
    try:
        data = json.loads(_BELIEFS_PATH.read_text(encoding="utf-8"))
        entries = data.get("beliefs") or []
    except Exception as exc:
        logger.error("[HealthStatusStage] Failed reading beliefs: %s", exc, exc_info=True)
        return ""

    active = [
        e for e in entries
        if e.get("statement")
        and e.get("status", "active") == "active"
        and e.get("domain", "") in _HEALTH_DOMAINS
        and e.get("belief_key", "") not in _EXCLUDED_BELIEF_KEYS
    ]

    active.sort(key=lambda e: _CONFIDENCE_RANK.get(e.get("confidence", "low"), 2))

    def _fmt(e: dict) -> str:
        stmt = e.get("statement", "")
        domain = e.get("domain", "")
        confidence = e.get("confidence", "")
        conditions = e.get("conditions", "")
        cond_suffix = f" [{conditions}]" if conditions else ""
        return f"[{domain}/{confidence}] {stmt}{cond_suffix}"

    return "\n".join(_fmt(e) for e in active) if active else "(no active health beliefs)"


def _format_sleep_output(ctx: StepContext) -> str:
    data = ctx.read_resource("resource_sleep_output.json")
    if not isinstance(data, dict):
        return ""
    parts: List[str] = []
    summary = data.get("summary") or data.get("sleep_summary") or ""
    if summary:
        parts.append(str(summary))
    total_hours = data.get("total_sleep_hours") or data.get("total_hours")
    if total_hours is not None:
        parts.append(f"Total sleep: {total_hours}h")
    quality = data.get("quality") or data.get("sleep_quality") or ""
    if quality:
        parts.append(f"Quality: {quality}")
    debt = data.get("sleep_debt") or data.get("cumulative_debt")
    if debt is not None:
        parts.append(f"Sleep debt: {debt}")
    return "\n".join(parts) if parts else ""


def _format_weekly_health(insights_data: Optional[Dict[str, Any]]) -> str:
    if not insights_data:
        return ""
    try:
        insights = insights_data.get("insights") or {}
        parts = []
        health_pattern = insights.get("health_pattern", "")
        if health_pattern:
            parts.append(f"Health pattern: {health_pattern}")
        sleep_pattern = insights.get("sleep_pattern", "")
        if sleep_pattern:
            parts.append(f"Sleep pattern: {sleep_pattern}")
        return "\n".join(parts)
    except Exception as exc:
        logger.debug("[HealthStatusStage] failed reading weekly insights: %s", exc)
        return ""


class HealthStatusStep(BaseStep):
    """
    Once-per-day stage that writes resource_user_health_status.md from belief engine
    health/sleep domain beliefs + today's sleep output.
    """

    step_id: str = "health_status"

    def _boundary_date_local(self, ctx: StepContext) -> str:
        return str(ctx.state.get("boundary_date_local") or ctx.now_local.strftime("%Y-%m-%d"))

    def _day_context_dir(self, ctx: StepContext, boundary_date_local: str) -> Path:
        year = boundary_date_local[:4]
        month = boundary_date_local[5:7]
        repo_root = Path(ctx.resources_dir).parent
        return repo_root / "day_context" / year / month / boundary_date_local

    def _already_generated_today(self, ctx: StepContext, boundary_date_local: str) -> bool:
        runs = ctx.state.get("step_runs", {})
        info = runs.get(self.step_id, {}) if isinstance(runs, dict) else {}
        if isinstance(info, dict) and info.get("last_generated_boundary") == boundary_date_local:
            latest_path = Path(ctx.resources_dir) / _LATEST_FILENAME
            archive_path = self._day_context_dir(ctx, boundary_date_local) / _LATEST_FILENAME
            return latest_path.exists() and archive_path.exists()
        return False

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        boundary_date_local = self._boundary_date_local(ctx)
        if self._already_generated_today(ctx, boundary_date_local):
            return False, f"already_generated boundary={boundary_date_local}"
        return True, "ready"

    def run(self, ctx: StepContext) -> StepResult:
        boundary_date_local = self._boundary_date_local(ctx)
        day_of_week = ctx.now_local.strftime("%A")

        health_beliefs_block = _format_health_beliefs()
        sleep_output_block = _format_sleep_output(ctx)

        weekly_insights_data = None
        if _WEEKLY_INSIGHTS_PATH.exists():
            try:
                weekly_insights_data = json.loads(_WEEKLY_INSIGHTS_PATH.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("[HealthStatusStage] failed reading weekly insights file: %s", exc)
        weekly_health_block = _format_weekly_health(weekly_insights_data)

        md = self._call_agent(
            boundary_date_local=boundary_date_local,
            day_of_week=day_of_week,
            health_beliefs_block=health_beliefs_block,
            sleep_output_block=sleep_output_block,
            weekly_health_block=weekly_health_block,
            ctx=ctx,
        )

        if not md:
            return StepResult(
                output={"status": "error", "error": "agent returned no markdown"},
            )

        md = md.strip() + "\n"
        latest_path = Path(ctx.resources_dir) / _LATEST_FILENAME
        archive_path = self._day_context_dir(ctx, boundary_date_local) / _LATEST_FILENAME

        write_text_atomic(latest_path, md)
        write_text_atomic(archive_path, md)

        try:
            from app.assistant.ServiceLocator.service_locator import DI
            if getattr(DI, "global_blackboard", None) is not None:
                DI.global_blackboard.update_state_value("resource_user_health_status", md)
        except Exception:
            logger.debug("[HealthStatusStage] could not push to global blackboard", exc_info=True)

        ctx.write_resource(
            _POINTER_FILENAME,
            {
                "schema_version": 1,
                "boundary_date_local": boundary_date_local,
                "day_of_week": day_of_week,
                "generated_at_utc": ctx.now_utc.isoformat(),
                "generated_at_local": ctx.now_local.strftime("%Y-%m-%d %H:%M"),
                "archive_path": str(archive_path),
            },
        )

        runs = ctx.state.setdefault("step_runs", {})
        info = runs.get(self.step_id)
        info_dict: Dict[str, Any] = info if isinstance(info, dict) else {}
        info_dict["last_generated_boundary"] = boundary_date_local
        info_dict["last_generated_at_utc"] = ctx.now_utc.isoformat()
        runs[self.step_id] = info_dict

        logger.info("[HealthStatusStage] written → %s", latest_path.name)
        return StepResult(
            output={"status": "ok", "boundary_date_local": boundary_date_local},
            debug={"latest_path": str(latest_path), "archive_path": str(archive_path), "chars": len(md)},
        )

    def _call_agent(
        self,
        *,
        boundary_date_local: str,
        day_of_week: str,
        health_beliefs_block: str,
        sleep_output_block: str,
        weekly_health_block: str,
        ctx: StepContext,
    ) -> Optional[str]:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message

            agent = DI.agent_factory.create_agent("health_status_writer")
            if agent is None:
                raise RuntimeError("health_status_writer agent not found")

            scope = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id="health_status_runner")

            msg = Message(
                scope_context=scope,
                agent_input={
                    "date_time": ctx.now_local.strftime("%Y-%m-%d %H:%M"),
                    "day_of_week": day_of_week,
                    "health_beliefs_block": health_beliefs_block,
                    "sleep_output_block": sleep_output_block,
                    "weekly_health_block": weekly_health_block,
                },
            )
            result = agent.action_handler(msg)
            data = getattr(result, "data", None)
            if isinstance(data, dict):
                return data.get("markdown")
        except Exception as exc:
            logger.error("[HealthStatusStage] agent call failed: %s", exc, exc_info=True)
        return None

    def reset(self, ctx: StepContext) -> None:
        runs = ctx.state.get("step_runs", {})
        if isinstance(runs, dict) and self.step_id in runs:
            runs[self.step_id] = {}

        # Clear stale health resource so yesterday's assessment doesn't
        # carry into today. The stage will regenerate on next run.
        from pathlib import Path
        latest = Path(ctx.resources_dir) / _LATEST_FILENAME
        pointer = Path(ctx.resources_dir) / _POINTER_FILENAME
        for path in (latest, pointer):
            try:
                if path.exists():
                    path.unlink()
                    logger.info("[HealthStatus] reset removed stale artifact: %s", path.name)
            except Exception as e:
                logger.error("[HealthStatus] reset failed to remove %s: %s", path.name, e)

        # Clear in-memory value so agents don't read stale data.
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            DI.global_blackboard.update_state_value("resource_user_health_status", "")
        except Exception:
            pass
