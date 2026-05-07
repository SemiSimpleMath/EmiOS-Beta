"""
DayFlow Routine Stage

Generates resource_dayflow_routine.md — a living, belief-enriched, context-aware
daily routine document. Runs hourly throughout the day.

- Reads resource_user_beliefs.json (belief engine export)
- Feeds daily_context_generator output so the agent knows what has already happened
- Generates from scratch each run (no previous version fed in)
- Feeds weekly insights flags for cross-day pattern awareness
- Writes resource_dayflow_routine.md (injected into all agents)
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc
from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context

from app.assistant.utils.path_utils import get_resources_dir as _get_resources_dir

logger = get_logger(__name__)

_LATEST_FILENAME = "resource_dayflow_routine.md"
_POINTER_FILENAME = "resource_dayflow_routine_latest.json"
_BELIEFS_PATH = _get_resources_dir() / "kg_derived" / "resource_user_beliefs.json"
_WEEKLY_INSIGHTS_PATH = _get_resources_dir() / "weekly_insights_pipeline_outputs" / "resource_weekly_insights_latest.json"

# Re-generate at most once per hour
_MIN_REGEN_INTERVAL_SECONDS = 3600


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _format_daily_context(ctx_data: Optional[Dict[str, Any]]) -> str:
    if not ctx_data:
        return "(no daily context available yet)"
    parts = []
    day_theme = ctx_data.get("day_theme", "")
    if day_theme:
        parts.append(f"Day theme: {day_theme}")
    expected = ctx_data.get("expected_schedule") or []
    if isinstance(expected, list) and expected:
        sched_lines = ["Expected schedule:"]
        for item in expected:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            start = item.get("start_local", "")
            end = item.get("end_local", "")
            status = item.get("status", "")
            time_range = f"{start} - {end}" if end else start
            sched_lines.append(f"  {title} {time_range} ({status})")
        parts.append("\n".join(sched_lines))
    elif isinstance(expected, str) and expected.strip():
        parts.append(f"Expected schedule:\n{expected}")
    current_status = ctx_data.get("current_status", "")
    if current_status:
        parts.append(f"Current status: {current_status}")
    milestones = ctx_data.get("milestones") or []
    if milestones:
        lines = ["Milestones (what has already happened):"]
        for m in milestones:
            flag = " [ongoing]" if m.get("ongoing") else ""
            lines.append(f"  {m.get('time', '?')} — {m.get('description', '')}{flag}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "(daily context empty)"


_DOMAIN_ORDER = ["routine", "health", "food", "general", "work"]

# Belief keys that must always appear prominently regardless of context.
# These are chronic, day-shaping facts that agents must never overlook.
_PINNED_BELIEF_KEYS = {
    "food.intermittent_fasting",
    "routine.intermittent_fasting",
    "health.intermittent_fasting",
    "health.back_pain",
    "health.finger_pain",
    "health.morning_post_nasal_drip",
    "routine.screen_free_cutoff",
    "routine.sleep_target",
    "routine.coffee_pacing",
    "food.coffee_cutoff_time_1600",
}

_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def _format_beliefs(day_of_week: str) -> str:
    if not _BELIEFS_PATH.exists():
        logger.error(
            "[DayFlowRoutine] Beliefs file not found at %s — routine will have no belief context.",
            _BELIEFS_PATH,
        )
        return ""
    try:
        data = json.loads(_BELIEFS_PATH.read_text(encoding="utf-8"))
        entries = data.get("beliefs") or []
    except Exception as exc:
        logger.error("[DayFlowRoutine] Failed reading beliefs: %s", exc, exc_info=True)
        return ""

    active = [e for e in entries if e.get("statement") and e.get("status", "active") == "active"]

    pinned = [e for e in active if e.get("belief_key", "") in _PINNED_BELIEF_KEYS]
    rest = [e for e in active if e.get("belief_key", "") not in _PINNED_BELIEF_KEYS]

    def _sort_key(e: dict) -> tuple:
        domain_rank = _DOMAIN_ORDER.index(e.get("domain", "")) if e.get("domain", "") in _DOMAIN_ORDER else len(_DOMAIN_ORDER)
        conf_rank = _CONFIDENCE_RANK.get(e.get("confidence", "low"), 2)
        return (domain_rank, conf_rank)

    pinned.sort(key=_sort_key)
    rest.sort(key=_sort_key)

    def _fmt(e: dict) -> str:
        stmt = e.get("statement", "")
        domain = e.get("domain", "")
        confidence = e.get("confidence", "")
        conditions = e.get("conditions", "")
        cond_suffix = f" [{conditions}]" if conditions else ""
        return f"[{domain}/{confidence}] {stmt}{cond_suffix}"

    parts: list[str] = []

    if pinned:
        parts.append("### Always-apply (chronic, high-impact — never omit these)")
        parts.extend(_fmt(e) for e in pinned)

    from collections import defaultdict
    by_domain: dict[str, list] = defaultdict(list)
    for e in rest:
        by_domain[e.get("domain", "other")].append(e)

    domain_order = _DOMAIN_ORDER + sorted(k for k in by_domain if k not in _DOMAIN_ORDER)
    for domain in domain_order:
        items = by_domain.get(domain)
        if not items:
            continue
        parts.append(f"\n### {domain.capitalize()} beliefs")
        parts.extend(_fmt(e) for e in items)

    return "\n".join(parts) if parts else "(no active beliefs)"


def _format_weekly_insights() -> str:
    if not _WEEKLY_INSIGHTS_PATH.exists():
        return ""
    try:
        data = json.loads(_WEEKLY_INSIGHTS_PATH.read_text(encoding="utf-8"))
        insights = data.get("insights") or {}
        week_start = str(data.get("week_start") or "").strip()
        week_end = str(data.get("week_end") or "").strip()
        parts = []
        if week_start and week_end:
            parts.append(
                f"(Covers last week: {week_start} through {week_end}. "
                f"Any bare weekday names refer to THAT past week, not the current week.)"
            )
        sleep_pattern = insights.get("sleep_pattern", "")
        if sleep_pattern:
            parts.append(f"Sleep pattern last week: {sleep_pattern}")
        health_pattern = insights.get("health_pattern", "")
        if health_pattern:
            parts.append(f"Health pattern last week: {health_pattern}")
        for bc in (insights.get("belief_candidates") or [])[:3]:
            parts.append(f"Weekly signal [{bc.get('domain','?')}]: {bc.get('statement','')}")
        return "\n".join(parts)
    except Exception as exc:
        logger.debug("[DayFlowRoutine] failed reading weekly insights: %s", exc)
        return ""


class DayFlowRoutineStep(BaseStep):
    """
    Hourly-regenerating, belief-enriched dayflow routine document.
    Writes resource_dayflow_routine.md — the canonical injected routine for all agents.
    """

    step_id: str = "dayflow_routine"

    def _boundary_date_local(self, ctx: StepContext) -> str:
        return str(ctx.state.get("boundary_date_local") or ctx.now_local.strftime("%Y-%m-%d"))

    def _day_context_dir(self, ctx: StepContext, boundary_date_local: str) -> Path:
        year = boundary_date_local[:4]
        month = boundary_date_local[5:7]
        repo_root = Path(ctx.resources_dir).parent
        return repo_root / "day_context" / year / month / boundary_date_local

    def _last_generated_at(self, ctx: StepContext) -> Optional[datetime]:
        runs = ctx.state.get("step_runs", {})
        info = runs.get(self.step_id, {}) if isinstance(runs, dict) else {}
        ts = info.get("last_generated_at_utc") if isinstance(info, dict) else None
        if not ts:
            return None
        # parse_iso_utc returns aware UTC for both Z-suffix and naive ISO,
        # keeping the (ctx.now_utc - last) subtraction in one tz state.
        return parse_iso_utc(ts)

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        boundary_date_local = self._boundary_date_local(ctx)
        latest_path = Path(ctx.resources_dir) / _LATEST_FILENAME
        if not latest_path.exists():
            return True, "missing_latest_markdown"

        pointer = ctx.read_resource(_POINTER_FILENAME)
        if not isinstance(pointer, dict):
            return True, "missing_latest_pointer"
        pointer_boundary = str(pointer.get("boundary_date_local") or "").strip()
        if pointer_boundary != boundary_date_local:
            return True, "pointer_boundary_mismatch"

        last = self._last_generated_at(ctx)
        if last is None:
            return True, "never_generated"
        elapsed = (ctx.now_utc - last).total_seconds()
        if elapsed >= _MIN_REGEN_INTERVAL_SECONDS:
            return True, f"interval_elapsed ({elapsed:.0f}s)"
        return False, f"too_soon ({elapsed:.0f}s < {_MIN_REGEN_INTERVAL_SECONDS}s)"

    def _read_daily_context(self, ctx: StepContext) -> Optional[Dict[str, Any]]:
        data = ctx.read_resource("resource_daily_context_generator_output.json")
        if not isinstance(data, dict):
            data = {}
        expected_calendar = ctx.read_resource("resource_expected_calendar.json")
        if isinstance(expected_calendar, dict):
            expected_schedule = expected_calendar.get("expected_schedule")
            if isinstance(expected_schedule, list) and expected_schedule:
                merged = dict(data)
                merged["expected_schedule"] = expected_schedule
                return merged
        return data if data else None

    def run(self, ctx: StepContext) -> StepResult:
        boundary_date_local = self._boundary_date_local(ctx)
        day_of_week = ctx.now_local.strftime("%A")

        daily_ctx_data = self._read_daily_context(ctx)
        daily_context_block = _format_daily_context(daily_ctx_data)
        beliefs_block = _format_beliefs(day_of_week)
        weekly_insights_block = _format_weekly_insights()
        md, change_summary = self._call_agent(
            boundary_date_local=boundary_date_local,
            day_of_week=day_of_week,
            daily_context_block=daily_context_block,
            beliefs_block=beliefs_block,
            weekly_insights_block=weekly_insights_block,
            ctx=ctx,
        )

        if not md:
            return StepResult(
                output={"status": "error", "error": "agent returned no markdown"},
            )

        md = md.strip() + "\n"
        latest_path = Path(ctx.resources_dir) / _LATEST_FILENAME
        archive_path = self._day_context_dir(ctx, boundary_date_local) / _LATEST_FILENAME

        _write_text_atomic(latest_path, md)
        _write_text_atomic(archive_path, md)

        try:
            from app.assistant.ServiceLocator.service_locator import DI
            if getattr(DI, "global_blackboard", None) is not None:
                DI.global_blackboard.update_state_value("resource_dayflow_routine", md)
        except Exception:
            logger.debug("dayflow_routine: could not push routine to global blackboard", exc_info=True)

        ctx.write_resource(
            _POINTER_FILENAME,
            {
                "schema_version": 1,
                "boundary_date_local": boundary_date_local,
                "day_of_week": day_of_week,
                "generated_at_utc": ctx.now_utc.isoformat(),
                "generated_at_local": ctx.now_local.strftime("%Y-%m-%d %H:%M"),
                "change_summary": change_summary,
                "archive_path": str(archive_path),
            },
        )

        runs = ctx.state.setdefault("step_runs", {})
        info = runs.get(self.step_id) if isinstance(runs.get(self.step_id), dict) else {}
        info["last_generated_at_utc"] = ctx.now_utc.isoformat()
        info["last_boundary"] = boundary_date_local
        runs[self.step_id] = info

        logger.info(
            "[DayFlowRoutine] written → %s | %s",
            latest_path.name, change_summary,
        )
        return StepResult(
            output={"status": "ok", "change_summary": change_summary},
            debug={"chars": len(md), "archive": str(archive_path)},
        )

    def _call_agent(
        self,
        *,
        boundary_date_local: str,
        day_of_week: str,
        daily_context_block: str,
        beliefs_block: str,
        weekly_insights_block: str,
        ctx: StepContext,
    ) -> Tuple[Optional[str], str]:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message

            agent = DI.agent_factory.create_agent("dayflow_routine_writer")
            if agent is None:
                raise RuntimeError("dayflow_routine_writer agent not found")

            scope = build_pipeline_scope_context(
                pipeline_id="dayflow",
                actor_id="dayflow_routine_runner",
            )

            msg = Message(
                scope_context=scope,
                agent_input={
                    "date_time": ctx.now_local.strftime("%Y-%m-%d %H:%M"),
                    "day_of_week": day_of_week,
                    "boundary_date_local": boundary_date_local,
                    "daily_context": daily_context_block,
                    "beliefs_block": beliefs_block,
                    "weekly_insights_block": weekly_insights_block,
                    "previous_routine_doc": "",
                },
            )
            result = agent.action_handler(msg)
            data = getattr(result, "data", None)
            if isinstance(data, dict):
                return data.get("markdown"), data.get("change_summary", "")
        except Exception as exc:
            logger.error("[DayFlowRoutine] agent call failed: %s", exc, exc_info=True)
        return None, ""

    def reset(self, ctx: StepContext) -> None:
        runs = ctx.state.get("step_runs", {})
        if isinstance(runs, dict) and self.step_id in runs:
            runs[self.step_id] = {}
        latest_path = Path(ctx.resources_dir) / _LATEST_FILENAME
        pointer_path = Path(ctx.resources_dir) / _POINTER_FILENAME
        for path in (latest_path, pointer_path):
            try:
                if path.exists():
                    path.unlink()
                    logger.info("[DayFlowRoutine] reset removed stale latest artifact: %s", path.name)
            except Exception as exc:
                logger.error("[DayFlowRoutine] reset failed removing %s: %s", path, exc)
                logger.debug("[DayFlowRoutine] reset removal exception details", exc_info=True)
                raise

        try:
            from app.assistant.ServiceLocator.service_locator import DI
            bb = getattr(DI, "global_blackboard", None)
            if bb is not None:
                bb.update_state_value("resource_dayflow_routine", "")
                logger.info("[DayFlowRoutine] reset cleared blackboard resource_dayflow_routine")
        except Exception as exc:
            logger.error("[DayFlowRoutine] reset failed clearing blackboard: %s", exc)
            logger.debug("[DayFlowRoutine] reset blackboard exception details", exc_info=True)
            raise
