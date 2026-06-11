from __future__ import annotations

from pathlib import Path
from typing import Any, List

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.routine_manager.utils import daily_insights_outputs_dir, read_json_optional, write_text_file
from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_timezone, get_local_time_str

from app.assistant.pipelines.context import PipelineContext


class ArchiveDailyAssessmentSummaryStep:
    name = "archive_daily_assessment_summary"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return [str(ctx.day_dir / "resource_daily_assessment.json"), "agent: daily_assessment_summary"]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return [
            ctx.day_dir / "resource_daily_assessment_summary.json",
            ctx.day_dir / "daily_assessment_summary.md",
        ]

    def run(self, ctx: PipelineContext) -> None:
        assessment_path = ctx.day_dir / "resource_daily_assessment.json"
        if not assessment_path.exists():
            raise RuntimeError(f"Missing daily assessment: {assessment_path}")

        assessment_text = assessment_path.read_text(encoding="utf-8")
        agent = DI.agent_factory.create_agent("daily_assessment_summary")
        if agent is None:
            raise RuntimeError("daily_assessment_summary agent unavailable")

        msg = Message(
            agent_input={
                "date_time": get_local_time_str(),
                "task": "Summarize the daily assessment bundle into a high-signal daily retrospective.",
                "daily_assessment_json": assessment_text,
            }
        )
        result = agent.action_handler(msg)
        payload: dict[str, Any] = {}
        if hasattr(result, "data") and isinstance(result.data, dict):
            payload = result.data
        elif isinstance(result, dict):
            payload = result

        summary_path = ctx.day_dir / "resource_daily_assessment_summary.json"
        write_json_atomic(
            summary_path,
            {
                "schema_version": 1,
                "date": ctx.date_str,
                "timezone": str(get_local_timezone()),
                "generated_at_utc": ctx.now_utc_iso(),
                "source_assessment": str(assessment_path),
                "summary": payload,
            },
        )

        latest_summary_payload = read_json_optional(summary_path) or {
            "schema_version": 1,
            "date": ctx.date_str,
            "timezone": str(get_local_timezone()),
            "generated_at_utc": ctx.now_utc_iso(),
            "source_assessment": str(assessment_path),
            "summary": payload,
        }
        if isinstance(latest_summary_payload, dict):
            latest_summary_payload["archive_path"] = str(summary_path)
            write_json_atomic(daily_insights_outputs_dir() / "resource_daily_assessment_summary.json", latest_summary_payload)

        # Markdown view (best-effort)
        title = str(payload.get("title") or f"Daily Assessment Summary -- {ctx.date_str}")
        bullets = payload.get("executive_summary") if isinstance(payload.get("executive_summary"), list) else []
        themes = payload.get("dominant_themes") if isinstance(payload.get("dominant_themes"), list) else []

        lines: list[str] = []
        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"- Date: {ctx.date_str}")
        lines.append(f"- Timezone: {get_local_timezone()}")
        lines.append(f"- Generated (UTC): {ctx.now_utc_iso()}")
        lines.append("")

        if bullets:
            lines.append("## Executive Summary")
            for b in bullets[:15]:
                lines.append(f"- {b}")
            lines.append("")

        if themes:
            lines.append("## Dominant Themes")
            for t in themes[:10]:
                if isinstance(t, dict):
                    theme = t.get("theme")
                    why = t.get("why_it_matters")
                    lines.append(f"- **{theme}**: {why}")
            lines.append("")

        md_text = "\n".join(lines)
        md_path = ctx.day_dir / "daily_assessment_summary.md"
        write_text_file(md_path, md_text)
        write_text_file(daily_insights_outputs_dir() / "resource_daily_assessment_summary_text.md", md_text)

