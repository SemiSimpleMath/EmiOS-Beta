"""
DayFlow Routine Stage

Generates resource_dayflow_routine.md - a belief-grounded LOGISTICS OVERLAY on
the day's schedule: a deterministic hour-by-hour scaffold spanning ~1h before
now through the end of the local day (built in
expected_calendar_utils.build_hour_grid from the structured calendar, so the
writer does no clock math), which the dayflow_routine_writer agent FILLS -
schedule items in their hours, clock-anchored beliefs (e.g. "stop cooling at
06:00", "AC to 70F at 21:00") in their own slots even with no event there, and
brief ramp guidance for the open hours.

Regeneration is delta-gated over calendar, active belief catalog, day theme,
current status, milestones and weekly insights. The existing hourly eligibility
limit still applies. An LLM selects contextually relevant beliefs; the writer
places their guidance in the day's slots. Neither stage runs on ordinary chat.

- Reads resource_user_beliefs.json (belief engine export)
- Reads resource_expected_calendar.json (structured UTC times)
- Feeds daily_context_generator output + weekly insights flags
- Writes resource_dayflow_routine.md (injected into all agents, incl. the planner)
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.assistant.utils.atomic_write import write_text_atomic
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc
from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.pipelines.dayflow.utils.expected_calendar_utils import (
    build_hour_grid,
    end_of_local_day_utc,
    render_hour_grid,
)
from app.assistant.scope.loader import load_scope_for_source

from app.assistant.utils.path_utils import get_resources_dir as _get_resources_dir

logger = get_logger(__name__)

_LATEST_FILENAME = "resource_dayflow_routine.md"
_POINTER_FILENAME = "resource_dayflow_routine_latest.json"
_BELIEFS_PATH = _get_resources_dir() / "kg_derived" / "resource_user_beliefs.json"
_WEEKLY_INSIGHTS_PATH = _get_resources_dir() / "weekly_insights_pipeline_outputs" / "resource_weekly_insights_latest.json"

# Re-generate at most once per hour
_MIN_REGEN_INTERVAL_SECONDS = 3600


_BACKDROP_MIN_HOURS = 4  # items longer than this are treated as backdrops

# ---- routine re-windowing (Option B) -------------------------------------------------------------------
# The routine doc is LLM markdown, cached + frozen by the delta gate (it regenerates on semantic input
# changes, never just as time passes). To keep the VISIBLE window current without re-running the LLM,
# _rewindow_cached_doc trims the cached doc to ~now-2h -> end-of-day on each no-delta run: drop the
# `### HH:MM` hour-sections whose LATEST time is already older than the cutoff. Deterministic, and biased
# toward KEEPING â€” it never drops a section with any part still in-window, keeps ## sections / preamble /
# unparseable headers, and is a pure no-op if the doc has no parseable hour grid.
_ROUTINE_LOOKBACK_HOURS = 2
_TIME_TOKEN_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", re.IGNORECASE)


def _latest_minutes(header: str) -> Optional[int]:
    """Max minutes-since-midnight among time tokens in a `###` header, or None if none parse. Morning hours
    are written 24h ('06:00'); afternoon 12h ('01:00 PM') â€” handle both. Range headers use the END (latest)."""
    best: Optional[int] = None
    for h, m, ap in _TIME_TOKEN_RE.findall(header):
        hh, mm = int(h), int(m)
        if hh > 23 or mm > 59:
            continue
        ap = ap.upper()
        if ap == "AM" and hh == 12:
            hh = 0
        elif ap == "PM" and hh != 12:
            hh += 12
        mins = hh * 60 + mm
        best = mins if best is None else max(best, mins)
    return best


def rewindow_routine(md: str, now_local: datetime, lookback_hours: int = _ROUTINE_LOOKBACK_HOURS) -> str:
    """Drop the routine's `### ...` hour-sections whose LATEST time is older than now-lookback_hours. Keeps
    ## sections, the preamble, ambiguous/unparseable `###` sections, and any section with part still in-window
    (bias toward KEEPING â€” never drops a future hour). Pure (md, now) -> md; a no-op if no hour grid parses."""
    cutoff = now_local.hour * 60 + now_local.minute - lookback_hours * 60
    if cutoff <= 0:
        return md  # early enough that nothing counts as 'past'
    out, drop = [], False
    for ln in md.splitlines():
        s = ln.lstrip()
        if s.startswith("### "):
            latest = _latest_minutes(s[4:])
            drop = latest is not None and latest < cutoff
            if drop:
                continue
        elif s.startswith("## "):
            drop = False  # a new top-level section ends any drop region
        if not drop:
            out.append(ln)
    result = "\n".join(out)
    if md.endswith("\n") and not result.endswith("\n"):
        result += "\n"  # preserve trailing newline so an unchanged doc compares equal (no needless re-write)
    return result


def _is_backdrop(item: dict) -> bool:
    """Long umbrella blocks like 'Work Hours' (7.5h) are *backdrops*, not
    actionable events. They're useful context (the writer should know
    the user is on the clock) but they shouldn't eat window slots that
    belong to specific upcoming items inside them."""
    start = parse_iso_utc(item.get("start_utc"))
    end = parse_iso_utc(item.get("end_utc"))
    if start is None or end is None:
        return False
    return (end - start) >= timedelta(hours=_BACKDROP_MIN_HOURS)


def _format_daily_context(
    ctx_data: Optional[Dict[str, Any]],
    *,
    now_utc: datetime,
) -> Tuple[str, str]:
    """Returns (daily_context_block, tail_anchors_block).

    Render the full remaining day and recent context; the legacy tail is empty.
    """
    if not ctx_data:
        return "(no daily context available yet)", ""
    parts: list[str] = []
    tail_anchors_block = ""

    day_theme = ctx_data.get("day_theme", "")
    if day_theme:
        parts.append(f"Day theme: {day_theme}")

    expected = ctx_data.get("expected_schedule") or []
    if isinstance(expected, str):
        if expected.strip():
            parts.append(f"Expected schedule:\n{expected}")
    else:
        # Full-day overlay: ~1h look-back through end of day. Split umbrella
        # backdrops (>=4h blocks like "Work Hours") out as context â€” they don't
        # eat hourly slots â€” and place the rest into the deterministic scaffold.
        lookback_cutoff = now_utc - timedelta(hours=1)
        specific, backdrops = [], []
        for item in expected:
            if not isinstance(item, dict):
                continue
            start = parse_iso_utc(item.get("start_utc"))
            end = parse_iso_utc(item.get("end_utc"))
            if start is None or end is None or end <= lookback_cutoff:
                continue
            (backdrops if _is_backdrop(item) else specific).append(item)

        if backdrops:
            backdrop_lines = ["Today's backdrops (umbrella blocks â€” context, not slots to fill):"]
            for item in sorted(backdrops, key=lambda x: str(x.get("start_utc") or "")):
                title = item.get("title", "")
                start = item.get("start_local", "")
                end = item.get("end_local", "")
                time_range = f"{start} - {end}" if end else start
                backdrop_lines.append(f"  {title} {time_range}")
            parts.append("\n".join(backdrop_lines))

        # Deterministic hour-by-hour scaffold: ~1h ago -> end of day, built from
        # the structured UTC times against `now`. The writer FILLS each slot
        # (including (open) hours); it never builds the grid or does clock math.
        slots = build_hour_grid(specific, now_utc, end_of_local_day_utc(now_utc))
        parts.append(render_hour_grid(slots))
        # No tail: the whole rest of the day is in-window.

    current_status = ctx_data.get("current_status", "")
    if current_status:
        parts.append(f"Current status: {current_status}")

    milestones = ctx_data.get("milestones") or []
    if milestones:
        lines = ["Milestones (what has already happened):"]
        for m in milestones:
            flag = " [ongoing]" if m.get("ongoing") else ""
            lines.append(f"  {m.get('time', '?')} â€” {m.get('description', '')}{flag}")
        parts.append("\n".join(lines))

    daily_context_block = "\n\n".join(parts) if parts else "(daily context empty)"
    return daily_context_block, tail_anchors_block


def _read_belief_entries() -> list[dict]:
    if not _BELIEFS_PATH.exists():
        raise FileNotFoundError(f"Belief export missing: {_BELIEFS_PATH}")
    data = json.loads(_BELIEFS_PATH.read_text(encoding="utf-8"))
    return [e for e in data.get("beliefs", [])
            if e.get("statement") and e.get("status", "active") == "active"]


def _format_beliefs(day_of_week: str) -> str:
    return _render_belief_block(_read_belief_entries())


def _render_belief_block(entries: list) -> str:
    """Preserve eligible beliefs and their qualifiers; relevance is an LLM decision."""
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    beliefs = sorted((e for e in entries if e.get("statement") and e.get("status", "active") == "active"),
                     key=lambda e: str(e.get("belief_key", "")))
    return _jinja_env.get_template("dayflow_routine_writer/prompts/beliefs.j2").render(beliefs=beliefs)


def _select_beliefs(entries: list[dict], daily_context: str, scope, *, weekly_insights: str = "") -> tuple[list[dict], str]:
    """The agent selects request-local IDs; Python restores exact original records."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message
    from app.assistant.agents.dayflow_belief_selector.agent_form import AgentForm
    by_key = {e["belief_key"]: e for e in entries}
    if len(by_key) != len(entries):
        raise ValueError("Duplicate belief keys in export")
    if not entries:
        return [], "Empty active belief catalog"
    from belief_engine.matching.selection import select_records
    def invoke(labeled, retry):
        agent = DI.agent_factory.create_agent("dayflow_belief_selector")
        if agent is None:
            raise RuntimeError("dayflow_belief_selector agent unavailable")
        result = agent.action_handler(Message(scope_context=scope, agent_input={
            "daily_context": daily_context, "belief_catalog": labeled,
            "weekly_insights": weekly_insights, "selection_retry": retry}))
        return getattr(result, "data", None)
    from belief_engine.matching.context import pages
    selected, reasons = [], []
    for page in pages(entries, max_chars=48000):
        chosen, reason = select_records(page, invoke, AgentForm)
        selected.extend(chosen)
        reasons.append(reason)
    return selected, "\n".join(reasons)


def _format_weekly_insights() -> str:
    if not _WEEKLY_INSIGHTS_PATH.exists():
        return ""
    try:
        data = json.loads(_WEEKLY_INSIGHTS_PATH.read_text(encoding="utf-8"))
        from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
        return _jinja_env.get_template("dayflow_routine_writer/prompts/weekly.j2").render(weekly=data)

    except Exception as exc:
        logger.debug("[DayFlowRoutine] failed reading weekly insights: %s", exc)
        return ""


def _routine_inputs_fingerprint(boundary_date_local: str, expected_schedule: list, beliefs_block: str,
                                *, daily_context: Optional[dict] = None, weekly_insights: str = "",
                                belief_catalog: Optional[list] = None) -> str:
    """Cache exact decision inputs, excluding wall-clock/scaffold rendering and generated timestamps."""
    context = daily_context or {}
    payload = {"projection_version": 2, "day": boundary_date_local,
               "schedule": expected_schedule, "beliefs": beliefs_block, "belief_catalog": belief_catalog,
               "day_theme": context.get("day_theme"), "current_status": context.get("current_status"),
               "milestones": context.get("milestones"), "weekly_insights": weekly_insights}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class DayFlowRoutineStep(BaseStep):
    """
    Hourly-eligible, delta-gated contextual belief projection.
    Writes resource_dayflow_routine.md â€” the canonical injected routine for all agents.
    """

    step_id: str = "dayflow_routine"

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
            if isinstance(expected_schedule, list):
                merged = dict(data)
                merged["expected_schedule"] = expected_schedule
                return merged
        return data if data else None

    def _rewindow_cached_doc(self, ctx) -> bool:
        """Re-trim the cached routine doc to ~now-2h->EOD and re-publish it to the SAME two surfaces the regen
        writes â€” the live file + the global blackboard â€” but NOT the day archive (that keeps the full-day
        record). Lets the visible window slide even when the delta gate skips regeneration. Returns True if it
        changed; never raises."""
        try:
            latest_path = Path(ctx.resources_dir) / _LATEST_FILENAME
            md = latest_path.read_text(encoding="utf-8")
            trimmed = rewindow_routine(md, ctx.now_local)
            if trimmed == md:
                return False
            write_text_atomic(latest_path, trimmed)
            try:
                from app.assistant.ServiceLocator.service_locator import DI
                if getattr(DI, "global_blackboard", None) is not None:
                    DI.global_blackboard.update_state_value("resource_dayflow_routine", trimmed)
            except Exception:
                logger.debug("dayflow_routine re-window: could not push to global blackboard", exc_info=True)
            return True
        except Exception as e:
            logger.error("[DayFlowRoutine] re-window failed: %s", e)
            logger.debug("[DayFlowRoutine] re-window exception", exc_info=True)
            return False

    def run(self, ctx: StepContext) -> StepResult:
        boundary_date_local = self._boundary_date_local(ctx)
        day_of_week = ctx.now_local.strftime("%A")

        daily_ctx_data = self._read_daily_context(ctx)
        daily_context_block, tail_anchors_block = _format_daily_context(
            daily_ctx_data, now_utc=ctx.now_utc,
        )
        belief_entries = _read_belief_entries()
        beliefs_block = _render_belief_block(belief_entries)
        weekly_insights_block = _format_weekly_insights()

        # Reuse only when the calendar, catalog and contextual decision inputs
        # are unchanged. Wall-clock passage alone only trims the visible window.
        expected_sched = (daily_ctx_data or {}).get("expected_schedule") or []
        fingerprint = _routine_inputs_fingerprint(boundary_date_local, expected_sched, beliefs_block,
                                                 daily_context=daily_ctx_data, weekly_insights=weekly_insights_block,
                                                 belief_catalog=belief_entries)
        prev_pointer = ctx.read_resource(_POINTER_FILENAME)
        prev_fp = prev_pointer.get("inputs_fingerprint") if isinstance(prev_pointer, dict) else None
        if prev_fp == fingerprint and (Path(ctx.resources_dir) / _LATEST_FILENAME).exists():
            # No LLM delta â€” but slide the VISIBLE window: re-trim the cached doc to ~now-2h->EOD so the dead
            # morning drops as the day advances (the doc is frozen by the gate; only the window should move).
            # Deterministic, no LLM, biased toward keeping.
            rewound = self._rewindow_cached_doc(ctx)
            logger.info("[DayFlowRoutine] skipped â€” no delta; doc re-windowed=%s.", rewound)
            return StepResult(output={"status": "skipped_no_delta", "rewound": rewound})

        scope = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id="dayflow_belief_selector")
        selected, selection_reason = _select_beliefs(belief_entries, daily_context_block, scope, weekly_insights=weekly_insights_block)
        selected_block = _render_belief_block(selected)

        md, change_summary = self._call_agent(
            boundary_date_local=boundary_date_local,
            day_of_week=day_of_week,
            daily_context_block=daily_context_block,
            tail_anchors_block=tail_anchors_block,
            beliefs_block=selected_block,
            weekly_insights_block=weekly_insights_block,
            ctx=ctx,
        )

        if not md:
            raise RuntimeError("dayflow_routine_writer returned no markdown; guidance was not refreshed")

        md = md.strip() + "\n"
        latest_path = Path(ctx.resources_dir) / _LATEST_FILENAME
        archive_path = ctx.day_archive_dir(boundary_date_local) / _LATEST_FILENAME

        write_text_atomic(latest_path, md)
        write_text_atomic(archive_path, md)

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
                "inputs_fingerprint": fingerprint,
                "selected_belief_keys": [e["belief_key"] for e in selected],
                "selection_reason": selection_reason,
            },
        )

        runs = ctx.state.setdefault("step_runs", {})
        info = runs.get(self.step_id) if isinstance(runs.get(self.step_id), dict) else {}
        info["last_generated_at_utc"] = ctx.now_utc.isoformat()
        info["last_boundary"] = boundary_date_local
        runs[self.step_id] = info

        logger.info(
            "[DayFlowRoutine] written â†’ %s | %s",
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
        tail_anchors_block: str,
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

            scope = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id="dayflow_routine_runner")

            msg = Message(
                scope_context=scope,
                agent_input={
                    "date_time": ctx.now_local.strftime("%Y-%m-%d %H:%M"),
                    "day_of_week": day_of_week,
                    "boundary_date_local": boundary_date_local,
                    "daily_context": daily_context_block,
                    "tail_anchors_block": tail_anchors_block,
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
