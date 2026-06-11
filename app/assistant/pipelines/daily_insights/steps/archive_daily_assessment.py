from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from app.assistant.routine_manager.utils import read_json_optional, write_latest_pointer_json
from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.time_utils import get_local_timezone

from app.assistant.pipelines.context import PipelineContext


class ArchiveDailyAssessmentStep:
    name = "archive_daily_assessment"

    def inputs(self, ctx: PipelineContext) -> List[str]:
        return [
            str(ctx.snapshots_dir / "resource_daily_context_generator_output.json"),
            str(ctx.snapshots_dir / "resource_sleep_output.json"),
            str(ctx.snapshots_dir / "resource_sleep_segments_output.json"),
            str(ctx.snapshots_dir / "resource_afk_statistics_output.json"),
            str(ctx.snapshots_dir / "resource_tracked_activities_output.json"),
            str(ctx.snapshots_dir / "resource_health_inference_output.json"),
            str(ctx.day_dir / "timeline_merged.json"),
            str(ctx.day_dir / "resource_daily_insights.json"),
            str(ctx.day_dir / "apply_daily_insights_results.json"),
            str(ctx.day_dir / "health_snapshots.json"),
            str(ctx.day_dir / "sleep_pre_sleep_snapshot.json"),
            str(ctx.day_dir / "tickets.json"),
        ]

    def outputs(self, ctx: PipelineContext) -> List[Path]:
        return [ctx.day_dir / "resource_daily_assessment.json"]

    def run(self, ctx: PipelineContext) -> None:
        def _read_snap(filename: str) -> Any:
            return read_json_optional(ctx.snapshots_dir / filename)

        def _read_day(filename: str) -> Any:
            return read_json_optional(ctx.day_dir / filename)

        daily_context = _read_snap("resource_daily_context_generator_output.json")
        sleep_output = _read_snap("resource_sleep_output.json")
        sleep_segments = _read_snap("resource_sleep_segments_output.json")
        afk_stats = _read_snap("resource_afk_statistics_output.json")
        tracked_activities = _read_snap("resource_tracked_activities_output.json")
        health_inference_latest = _read_snap("resource_health_inference_output.json")

        timeline_payload = _read_day("timeline_merged.json")
        daily_insights = _read_day("resource_daily_insights.json")
        apply_results = _read_day("apply_daily_insights_results.json")
        health_snapshots = _read_day("health_snapshots.json")
        sleep_pre_snapshot = _read_day("sleep_pre_sleep_snapshot.json")
        tickets_export = _read_day("tickets.json")

        # --- Health snapshots compression (string-valued fields) ---
        def _extract_health_fields(h: dict) -> dict[str, Optional[str]]:
            mental = h.get("mental") if isinstance(h.get("mental"), dict) else {}
            cognitive = h.get("cognitive") if isinstance(h.get("cognitive"), dict) else {}
            physical = h.get("physical") if isinstance(h.get("physical"), dict) else {}
            physiology = h.get("physiology") if isinstance(h.get("physiology"), dict) else {}
            fields = {
                "mental.mood": mental.get("mood"),
                "mental.stress_load": mental.get("stress_load"),
                "mental.anxiety": mental.get("anxiety"),
                "mental.mental_energy": mental.get("mental_energy"),
                "mental.social_capacity": mental.get("social_capacity"),
                "cognitive.load": cognitive.get("load"),
                "cognitive.interruption_tolerance": cognitive.get("interruption_tolerance"),
                "cognitive.focus_depth": cognitive.get("focus_depth"),
                "physical.energy_level": physical.get("energy_level"),
                "physical.pain_level": physical.get("pain_level"),
                "physiology.hunger_probability": physiology.get("hunger_probability"),
                "physiology.hydration_need": physiology.get("hydration_need"),
                "physiology.caffeine_state": physiology.get("caffeine_state"),
            }
            out: dict[str, Optional[str]] = {}
            for k, v in fields.items():
                out[k] = str(v) if v is not None else None
            return out

        def _mode(values: list[Optional[str]]) -> Optional[str]:
            counts: dict[str, int] = {}
            for v in values:
                if not v:
                    continue
                counts[v] = counts.get(v, 0) + 1
            if not counts:
                return None
            return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

        health_shape: dict[str, Any] = {"available": False}
        if isinstance(health_snapshots, dict) and isinstance(health_snapshots.get("snapshots"), list):
            snaps = [s for s in health_snapshots.get("snapshots") if isinstance(s, dict)]
            series: dict[str, list[Optional[str]]] = {}
            transitions: dict[str, list[str]] = {}
            first: dict[str, Optional[str]] = {}
            last: dict[str, Optional[str]] = {}
            for s in snaps:
                h = s.get("health")
                if not isinstance(h, dict):
                    continue
                fields = _extract_health_fields(h)
                for k, v in fields.items():
                    series.setdefault(k, []).append(v)
            for k, vals in series.items():
                if vals:
                    first[k] = vals[0]
                    last[k] = vals[-1]
                seq: list[str] = []
                prev: Optional[str] = None
                for v in vals:
                    if not v:
                        continue
                    if v == prev:
                        continue
                    seq.append(v)
                    prev = v
                transitions[k] = seq
            health_shape = {
                "available": True,
                "snapshot_count": len(snaps),
                "baseline_mode": {k: _mode(v) for k, v in series.items()},
                "first": first,
                "last": last,
                "transitions": transitions,
                "volatility": {k: max(0, len(seq) - 1) for k, seq in transitions.items()},
            }

        counts: dict[str, Any] = {}
        if isinstance(timeline_payload, dict):
            counts.update(timeline_payload.get("counts") or {})
            counts["timeline_items"] = len(timeline_payload.get("timeline") or []) if isinstance(timeline_payload.get("timeline"), list) else None
        if isinstance(tickets_export, dict):
            counts["tickets_export_count"] = tickets_export.get("count")
        if isinstance(apply_results, dict):
            counts["insights_applied_count"] = apply_results.get("count")

        out_path = ctx.day_dir / "resource_daily_assessment.json"
        write_json_atomic(
            out_path,
            {
                "schema_version": 1,
                "date": ctx.date_str,
                "timezone": str(get_local_timezone()),
                "generated_at_utc": ctx.now_utc_iso(),
                "paths": {
                    "folder": str(ctx.day_dir),
                    "resource_snapshots": str(ctx.snapshots_dir),
                    "timeline_merged": str(ctx.day_dir / "timeline_merged.json"),
                    "daily_insights": str(ctx.day_dir / "resource_daily_insights.json"),
                    "apply_daily_insights_results": str(ctx.day_dir / "apply_daily_insights_results.json"),
                    "health_snapshots": str(ctx.day_dir / "health_snapshots.json"),
                    "sleep_pre_sleep_snapshot": str(ctx.day_dir / "sleep_pre_sleep_snapshot.json"),
                    "tickets_export": str(ctx.day_dir / "tickets.json"),
                },
                "counts": counts,
                "daily_context_snapshot": daily_context,
                "sleep_output_snapshot": sleep_output,
                "sleep_segments_snapshot": sleep_segments,
                "sleep_pre_sleep_snapshot": sleep_pre_snapshot,
                "afk_statistics_snapshot": afk_stats,
                "tracked_activities_snapshot": tracked_activities,
                "health_inference_snapshot": health_inference_latest,
                "health_snapshots": health_snapshots,
                "health_shape": health_shape,
                "timeline_merged": timeline_payload,
                "daily_insights": daily_insights,
                "apply_daily_insights_results": apply_results,
                "tickets_export": tickets_export,
                "notes": "Deterministic daily assessment bundle (5AM boundary-day). Uses day_context snapshots/artifacts for crash safety.",
            },
        )

        write_latest_pointer_json(
            resource_filename="resource_daily_assessment_latest.json",
            date_str=ctx.date_str,
            archive_path=out_path,
            extra={"counts": counts},
        )

