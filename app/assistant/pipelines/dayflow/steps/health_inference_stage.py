# stages/health_inference_stage.py
"""
Health Inference Stage

Flow:
1. Load stage config, check gate conditions
2. Prepare inputs: Build context items (time_since, calendar_events, etc.)
3. Execute: Run health_status_inference agent
4. Output: Write resource_health_inference_output.json

Notes:
- This is an agent-based stage (calls LLM) so it should be gated by AFK and interval.
- Output includes both LOCAL display strings and UTC ISO strings for downstream math.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import parse_iso_utc
from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.pipelines.dayflow.utils.context_sources import (
    build_time_since,
    get_calendar_events_for_health_inference,
    get_responded_tickets_categorized,
)
from app.assistant.pipelines.dayflow.utils.room_scope import resolve_room_scope
from app.assistant.utils.chat_formatting import messages_to_chat_history_text
from app.assistant.scope.loader import load_scope_for_source

logger = get_logger(__name__)


class HealthInferenceStep(BaseStep):
    def _build_pipeline_scope_context(self, agent_input: Dict[str, Any]):
        history_scope = agent_input.get("history_scope") if isinstance(agent_input, dict) else {}
        room_id = str(history_scope.get("room_id") or "").strip() if isinstance(history_scope, dict) else ""
        room_surface = str(history_scope.get("room_surface") or "").strip() if isinstance(history_scope, dict) else ""
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=f"{self.step_id}_runner", identity_overrides={"room_id": room_id or None, "surface": (room_surface or None) or "pipeline"})

    """
    Pipeline stage that runs the health_status_inference agent to infer
    user's health/energy/cognitive state from available data.
    """

    step_id: str = "health_inference"

    # -------------------------------------------------------------------------
    # Config & Cursor
    # -------------------------------------------------------------------------

    def _output_filename(self) -> str:
        return "resource_health_inference_output.json"

    def _boundary_date_local(self, ctx: StepContext) -> str:
        """
        Use the pipeline manager's boundary-day (default 5AM local).
        Falls back to local calendar date if boundary_date_local is missing.
        """
        return str(ctx.state.get("boundary_date_local") or ctx.now_local.strftime("%Y-%m-%d"))

    def _snapshots_path(self, ctx: StepContext, boundary_date_local: str) -> Path:
        year = boundary_date_local[:4]
        month = boundary_date_local[5:7]
        # Archive per-day artifacts outside `resources/` so resources remains for injection.
        repo_root = Path(ctx.resources_dir).parent
        return (
            repo_root
            / "day_context"
            / year
            / month
            / boundary_date_local
            / "health_snapshots.json"
        )

    @staticmethod
    def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _load_snapshots_file(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _maybe_append_snapshot(
        self,
        ctx: StepContext,
        *,
        boundary_date_local: str,
        health_snapshot: Dict[str, Any],
    ) -> Tuple[bool, int]:
        """
        Append a health snapshot into day_context/.../health_snapshots.json.
        Rate-limited by snapshot_policy.min_interval_seconds.
        """
        stage_cfg = ctx.step_config or {}
        snapshot_policy = stage_cfg.get("snapshot_policy", {}) if isinstance(stage_cfg, dict) else {}
        min_interval = int(snapshot_policy.get("min_interval_seconds", 2 * 60 * 60))  # default 2h
        max_snapshots = int(snapshot_policy.get("max_snapshots_per_day", 24))

        path = self._snapshots_path(ctx, boundary_date_local)
        payload = self._load_snapshots_file(path)
        existing = payload.get("snapshots") if isinstance(payload, dict) else None
        snapshots: List[Dict[str, Any]] = existing if isinstance(existing, list) else []

        if snapshots:
            last_ts = snapshots[-1].get("timestamp_utc")
            last_dt = parse_iso_utc(last_ts) if isinstance(last_ts, str) else None
            if last_dt is not None:
                elapsed = (ctx.now_utc - last_dt).total_seconds()
                if elapsed < min_interval:
                    return False, len(snapshots)

        snapshots.append(health_snapshot)
        if max_snapshots > 0 and len(snapshots) > max_snapshots:
            snapshots = snapshots[-max_snapshots:]

        out = {
            "schema_version": 1,
            "boundary_date_local": boundary_date_local,
            "timezone": str(ctx.now_local.tzinfo),
            "last_updated_utc": ctx.now_utc.isoformat(),
            "count": len(snapshots),
            "snapshots": snapshots,
        }
        self._write_json_atomic(path, out)
        return True, len(snapshots)

    def _get_last_run_utc(self, ctx: StepContext) -> Optional[datetime]:
        """Get this step's last run timestamp from state."""
        step_runs = ctx.state.get("step_runs", {})
        step_info = step_runs.get(self.step_id, {}) or {}
        stage_info = step_info
        return parse_iso_utc(stage_info.get("last_run_utc"))

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
        stage_cfg = ctx.step_config or {}
        run_policy = stage_cfg.get("run_policy", {}) if isinstance(stage_cfg, dict) else {}
        min_interval = int(run_policy.get("min_interval_seconds", 300))  # Default 5 min for agent stages

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
            if afk_guard.get("skip_when_potentially_afk", False) and is_potentially_afk:
                return False, "afk_guard=potentially_afk"

        return True, "ready"

    # -------------------------------------------------------------------------
    # Context building helpers
    # -------------------------------------------------------------------------

    def _get_todo_tasks(self) -> List[Dict[str, Any]]:
        """Get todo/tasks from event repository."""
        try:
            from app.assistant.event_repository.event_repository import EventRepositoryManager
            import json as _json

            repo = EventRepositoryManager()
            tasks_json = repo.search_events(data_type="todo_task")
            tasks_list = _json.loads(tasks_json) if tasks_json else []

            result = []
            for task in tasks_list:
                data = task.get("data", {})
                if isinstance(data, str):
                    try:
                        data = _json.loads(data)
                    except Exception:
                        continue
                if isinstance(data, dict):
                    result.append(
                        {
                            "title": data.get("title", data.get("name", "")),
                            "completed": data.get("completed", data.get("done", False)),
                        }
                    )
            return result
        except Exception as e:
            logger.warning(f"Could not get tasks: {e}")
            return []

    def _get_recent_chat_history(self, ctx: StepContext, *, hours: int) -> str:
        try:
            from app.assistant.ServiceLocator.service_locator import DI

            cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=int(hours))
            scope = resolve_room_scope(pipeline_config=ctx.pipeline_config, step_config=ctx.step_config)
            msgs = DI.global_blackboard.get_recent_chat_since_utc(
                cutoff_utc,
                limit=None,
                room_id=scope["room_id"],
                room_surface=scope["room_surface"],
                shared_room_ids=scope.get("shared_chat_room_ids", []),
            )
            return messages_to_chat_history_text(msgs, consumer_room_id=scope["room_id"])
        except Exception:
            return ""

    # -------------------------------------------------------------------------
    # Agent call
    # -------------------------------------------------------------------------

    def _call_agent(self, agent_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Call the health_status_inference agent."""
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message

            agent = DI.agent_factory.create_agent("health_status_inference")
            scope_context = self._build_pipeline_scope_context(agent_input)
            result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope_context))

            if hasattr(result, "data") and isinstance(result.data, dict):
                return result.data
            elif isinstance(result, dict):
                return result
            else:
                return {"result": str(result)}

        except Exception as e:
            logger.error(f"Error calling health_status_inference agent: {e}")
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
        boundary_date_local = self._boundary_date_local(ctx)

        # Step 1: Build context items that the agent needs
        stage_config = ctx.step_config or {}
        lookbacks = stage_config.get("lookback_hours", {}) if isinstance(stage_config, dict) else {}
        chat_hours = int(lookbacks.get("chat_history", 2))
        tickets_hours = int(lookbacks.get("recent_tickets", 2))

        # Get day_start for tickets context
        day_start_utc = parse_iso_utc(ctx.state.get("day_start_time_utc"))
        if not day_start_utc:
            day_start_utc = now_utc - timedelta(hours=8)

        calendar_events = get_calendar_events_for_health_inference()
        todo_tasks = self._get_todo_tasks()

        context = {
            "time_since": build_time_since(),
            "calendar_events": calendar_events,
            "recent_responded_tickets": get_responded_tickets_categorized(
                since_utc=now_utc - timedelta(hours=tickets_hours)
            ),
            "todo": todo_tasks,
            "recent_chat_history": self._get_recent_chat_history(ctx, hours=chat_hours),
            "history_scope": resolve_room_scope(pipeline_config=ctx.pipeline_config, step_config=ctx.step_config),
            "meetings_today": len([e for e in calendar_events if e.get("is_meeting")]),
            "tasks": todo_tasks,  # Alias for template compatibility
        }

        # Step 2: Execute agent
        output = self._call_agent(context)

        if not output:
            logger.warning("HealthInferenceStep: Agent returned no output")
            return StepResult(
                output={"error": "Agent returned no output"},
                debug={},
            )

        # Step 3: Build and write output
        stage_output: Dict[str, Any] = {
            **output,
            "date": boundary_date_local,
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),
            "last_updated_utc": now_utc.isoformat(),
        }

        ctx.write_resource(self._output_filename(), stage_output)

        # Persist time-series snapshots (rate-limited).
        snapshot_written = False
        snapshot_count = 0
        try:
            snapshot_written, snapshot_count = self._maybe_append_snapshot(
                ctx,
                boundary_date_local=boundary_date_local,
                health_snapshot={
                    "boundary_date_local": boundary_date_local,
                    "timestamp_utc": now_utc.isoformat(),
                    "timestamp_local": now_local.strftime("%Y-%m-%d %H:%M"),
                    "timestamp_local_full": now_local.strftime("%Y-%m-%d %I:%M %p %Z"),
                    "health": stage_output,
                },
            )
        except Exception as e:
            logger.warning(f"HealthInferenceStep: failed to append snapshot: {e}")
        else:
            # Write a small "latest pointer" into resources/ for injection/debugging.
            if snapshot_written:
                try:
                    pointer_path = Path(ctx.resources_dir) / "resource_health_snapshots_latest.json"
                    self._write_json_atomic(
                        pointer_path,
                        {
                            "schema_version": 1,
                            "boundary_date_local": boundary_date_local,
                            "timestamp_utc": now_utc.isoformat(),
                            "timestamp_local": now_local.strftime("%Y-%m-%d %H:%M"),
                            "archive_path": str(self._snapshots_path(ctx, boundary_date_local)),
                            "count": snapshot_count,
                        },
                    )
                except Exception as e:
                    logger.debug("HealthInferenceStep: failed to write latest pointer: %s", e)

        # Log key health indicators if present
        energy = output.get("energy_level", output.get("energy", "unknown"))
        cognitive = output.get("cognitive_load", output.get("cognitive", "unknown"))
        logger.info(f"HealthInferenceStep: energy={energy}, cognitive={cognitive}")

        return StepResult(
            output=stage_output,
            debug={
                "output_keys": list(output.keys())[:10],
                "snapshot_written": bool(snapshot_written),
                "snapshot_count": int(snapshot_count),
            },
        )

    def reset(self, ctx: StepContext) -> None:
        """Clear stale health inference resource at daily boundary."""
        runs = ctx.state.get("step_runs", {})
        if isinstance(runs, dict) and self.step_id in runs:
            runs[self.step_id] = {}

        from pathlib import Path
        output_path = Path(ctx.resources_dir) / self._output_filename()
        try:
            if output_path.exists():
                output_path.unlink()
                logger.info("[HealthInference] reset removed stale artifact: %s", output_path.name)
        except Exception as e:
            logger.error("[HealthInference] reset failed to remove %s: %s", output_path.name, e)

