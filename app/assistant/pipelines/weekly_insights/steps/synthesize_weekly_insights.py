from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

from app.assistant.utils.path_utils import get_resources_dir as _get_resources_dir
from app.assistant.utils.logging_config import get_logger
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_WEEKLY_OUTPUTS_DIR = _get_resources_dir() / "weekly_insights_pipeline_outputs"
_AGENT_NAME = "weekly_synthesizer"


def _format_summaries_block(summaries: List[dict]) -> str:
    """Render collected daily summaries as a readable text block for the agent prompt."""
    parts = []
    for entry in summaries:
        d = entry.get("date", "?")
        s = entry.get("summary") or {}
        title = s.get("title", "")
        exec_lines = "\n".join(f"  - {b}" for b in (s.get("executive_summary") or []))
        themes = ", ".join(t.get("theme", "") for t in (s.get("dominant_themes") or []))
        sleep_sum = (s.get("sleep_summary") or {}).get("summary", "")
        health_shape_baseline = (s.get("health_day_shape") or {}).get("baseline", "")
        health_shape_trend = (s.get("health_day_shape") or {}).get("trend", "")
        work_sum = (s.get("work_presence_summary") or {}).get("summary", "")
        mem_sum = (s.get("memory_updates") or {}).get("summary", "")
        parts.append(
            f"=== {d}: {title} ===\n"
            f"Executive summary:\n{exec_lines}\n"
            f"Themes: {themes}\n"
            + (f"Sleep: {sleep_sum}\n" if sleep_sum else "")
            + (f"Health baseline: {health_shape_baseline}\n" if health_shape_baseline else "")
            + (f"Health trend: {health_shape_trend}\n" if health_shape_trend else "")
            + (f"Work: {work_sum}\n" if work_sum else "")
            + (f"Memory updates: {mem_sum}\n" if mem_sum else "")
        )
    return "\n".join(parts)


class SynthesizeWeeklyInsightsStep:
    """
    Calls the weekly_synthesizer agent with the collected daily summaries
    and writes resource_weekly_insights.json to the week_dir.
    """

    name = "synthesize_weekly_insights"

    def inputs(self, ctx: Any) -> List[str]:
        return [str(ctx.week_dir / "daily_summaries_collected.json")]

    def outputs(self, ctx: Any) -> List[Path]:
        return [ctx.week_dir / "resource_weekly_insights.json"]

    def run(self, ctx: Any) -> dict:
        collected_path = ctx.week_dir / "daily_summaries_collected.json"
        if not collected_path.exists():
            raise RuntimeError(f"[SynthesizeWeeklyInsights] Missing collected summaries: {collected_path}")

        collected = json.loads(collected_path.read_text(encoding="utf-8"))
        summaries = collected.get("days") or []
        if not summaries:
            raise ValueError("[SynthesizeWeeklyInsights] No daily summaries to synthesize.")

        summaries_block = _format_summaries_block(summaries)
        week_label = _make_week_label(summaries)

        agent = DI.agent_factory.create_agent(_AGENT_NAME)
        if agent is None:
            raise RuntimeError(f"[SynthesizeWeeklyInsights] Agent '{_AGENT_NAME}' not found")

        scope = build_pipeline_scope_context(
            pipeline_id="weekly_insights",
            actor_id="weekly_synthesizer_runner",
        )
        msg = Message(
            scope_context=scope,
            agent_input={
                "task": f"Synthesize weekly insights for {week_label}.",
                "weekly_summaries_block": summaries_block,
            }
        )
        result = agent.action_handler(msg)

        payload: dict = {}
        if hasattr(result, "data") and isinstance(result.data, dict):
            payload = result.data
        elif isinstance(result, dict):
            payload = result

        out = {
            "schema_version": 1,
            "week_end": ctx.end_date_str,
            "week_start": summaries[0]["date"] if summaries else "",
            "days_included": [s["date"] for s in summaries],
            "days_missing": collected.get("missing") or [],
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "insights": payload,
        }

        out_path = ctx.week_dir / "resource_weekly_insights.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("[SynthesizeWeeklyInsights] Written → %s", out_path)

        # Write latest pointer
        _WEEKLY_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        latest = dict(out)
        latest["archive_path"] = str(out_path)
        (_WEEKLY_OUTPUTS_DIR / "resource_weekly_insights_latest.json").write_text(
            json.dumps(latest, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        return {"week_label": payload.get("week_label", week_label), "out_path": str(out_path)}


def _make_week_label(summaries: List[dict]) -> str:
    if not summaries:
        return "Unknown week"
    start = summaries[0].get("date", "")
    end = summaries[-1].get("date", "")
    try:
        from datetime import date
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
        return f"Week of {s.strftime('%b')} {s.day}–{e.day}, {e.year}"
    except Exception:
        return f"Week of {start} – {end}"
