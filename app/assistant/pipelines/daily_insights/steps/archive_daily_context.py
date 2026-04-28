from __future__ import annotations

import json
from pathlib import Path
from typing import List

from app.assistant.routine_manager.utils import daily_insights_outputs_dir, read_json_file, write_text_file

from app.assistant.pipelines.context import PipelineContext


class ArchiveDailyContextStep:
    name = "archive_daily_context"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return [
            str(ctx.snapshots_dir / "resource_daily_context_generator_output.json"),
            str(ctx.snapshots_dir / "resource_expected_calendar.json"),
        ]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return [ctx.day_dir / "daily_context.md"]

    def run(self, ctx: PipelineContext) -> None:
        src_path = ctx.snapshots_dir / "resource_daily_context_generator_output.json"
        data = read_json_file(src_path)
        if not isinstance(data, dict):
            raise RuntimeError(f"Daily context snapshot missing or invalid: {src_path}")

        date_str = ctx.date_str
        expected_calendar_path = ctx.snapshots_dir / "resource_expected_calendar.json"
        expected_calendar = read_json_file(expected_calendar_path)
        if not isinstance(expected_calendar, dict):
            raise RuntimeError(f"Expected calendar snapshot missing or invalid: {expected_calendar_path}")

        raw_schedule = expected_calendar.get("expected_schedule")
        if isinstance(raw_schedule, list):
            sched_lines = []
            for item in raw_schedule:
                if not isinstance(item, dict):
                    continue
                title = item.get("title", "")
                start = item.get("start_local", "")
                end = item.get("end_local", "")
                status = item.get("status", "")
                time_range = f"{start} - {end}" if end else start
                sched_lines.append(f"- {title} {time_range} ({status})")
            expected_schedule = "\n".join(sched_lines)
        else:
            expected_schedule = str(raw_schedule or "").strip()
        day_theme = str(data.get("day_theme") or "").strip()
        current_status = str(data.get("current_status") or "").strip()
        milestones = data.get("milestones") if isinstance(data.get("milestones"), list) else []
        last_updated = str(data.get("last_updated") or "").strip()
        last_updated_utc = str(data.get("last_updated_utc") or "").strip()

        lines = [
            f"# Daily Context -- {date_str}",
            "",
        ]
        if last_updated:
            lines.append(f"- Last updated (local): {last_updated}")
        if last_updated_utc:
            lines.append(f"- Last updated (UTC): {last_updated_utc}")
        if last_updated or last_updated_utc:
            lines.append("")

        if day_theme:
            lines.append("## Day Theme")
            lines.append(day_theme)
            lines.append("")

        if expected_schedule:
            lines.append("## Expected Schedule")
            lines.append(expected_schedule)
            lines.append("")

        if milestones:
            lines.append("## Milestones")
            for item in milestones:
                lines.append(f"- {item}")
            lines.append("")

        if current_status:
            lines.append("## Current Status")
            lines.append(current_status)
            lines.append("")

        lines.append("## Raw Source")
        lines.append("```json")
        lines.append(json.dumps(data, indent=2, ensure_ascii=True))
        lines.append("```")
        lines.append("")

        md_text = "\n".join(lines)
        out_path = ctx.day_dir / "daily_context.md"
        write_text_file(out_path, md_text)
        write_text_file(daily_insights_outputs_dir() / "resource_daily_context.md", md_text)
