from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("Failed to read JSON file: %s (%s)", path, e)
        return None


@dataclass
class StepResult:
    """
    Result returned by a DayFlow step's `run()` method.

    Steps write their own output files via ctx.write_resource().
    StepResult is for communicating state updates and debug info back to the runner.
    """

    output: Optional[Dict[str, Any]] = None
    state_updates: Dict[str, Any] = field(default_factory=dict)
    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StepContext:
    """
    Execution context passed to DayFlow steps.

    IMPORTANT:
    - Steps should not write directly to files under `resources/` using hard-coded globals.
      Use `write_resource()` so outputs are mirrored into `day_context/.../resource_snapshots/`.
    """

    now_utc: Any
    now_local: Any
    state: Dict[str, Any]
    pipeline_config: Dict[str, Any]
    step_config: Dict[str, Any]
    resources_dir: Path
    day_dir: Path
    new_chat_messages: List[Dict[str, Any]] = field(default_factory=list)

    def day_archive_dir(self, boundary_date_local: str) -> Path:
        """Per-day archive dir used by step-local snapshots / markdown archives.

        NOTE (dedup audit 2026-06-10): this resolves under
        ``resources/day_context/...`` — a SEPARATE tree from ``day_dir``
        (``<repo_root>/day_context/...``). Every step helper this replaced
        used the resources-rooted tree, so behavior is preserved as-is;
        unifying the two trees is a data-migration decision.
        """
        year = boundary_date_local[:4]
        month = boundary_date_local[5:7]
        return Path(self.resources_dir).parent / "day_context" / year / month / boundary_date_local

    def read_resource(self, filename: str) -> Optional[Dict[str, Any]]:
        return _read_json_file(self.resources_dir / filename)

    def write_resource(self, filename: str, data: Dict[str, Any]) -> None:
        """
        Write a resource file and mirror it into the day_context archive.

        Mirror policy:
        - Always write to `resources/<filename>` (latest for injections).
        - If filename looks like a normal resource output (`resource_*.json`), also mirror to:
          `day_context/.../<boundary_date>/resource_snapshots/<filename>`.
        """
        # 1) Latest output (injection source)
        write_json_atomic(self.resources_dir / filename, data)

        # 2) Archive mirror (best-effort)
        try:
            if filename.startswith("resource_") and filename.lower().endswith(".json"):
                snap_dir = self.day_dir / "resource_snapshots"
                write_json_atomic(snap_dir / filename, data)
        except Exception:
            # Mirroring is best-effort; pipeline must keep running.
            pass

        # 3) In-memory resource cache update (best-effort)
        try:
            resource_manager = getattr(DI, "resource_manager", None)
            if resource_manager:
                resource_id = Path(filename).stem
                resource_manager.update_resource(resource_id, data, persist=False)
        except Exception:
            pass


def format_belief_line(e: dict) -> str:
    """One belief as ``[domain/confidence] statement [conditions]`` — the
    shared rendering used by the health-status and dayflow-routine stages."""
    stmt = e.get("statement", "")
    domain = e.get("domain", "")
    confidence = e.get("confidence", "")
    conditions = e.get("conditions", "")
    cond_suffix = f" [{conditions}]" if conditions else ""
    return f"[{domain}/{confidence}] {stmt}{cond_suffix}"


class BaseStep:
    """
    Base class for DayFlow steps.
    """

    step_id: str = ""

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        return True, "ready"

    # ------------------------------------------------------------------
    # Shared step helpers (dedup audit 2026-06-10 — were copy-pasted
    # across six-plus stages).
    # ------------------------------------------------------------------

    def _build_pipeline_scope_context(
        self,
        agent_input: Optional[Dict[str, Any]] = None,
        *,
        history_scope: Optional[Dict[str, Any]] = None,
    ):
        """Dayflow pipeline scope for this step's agent invocations.

        Pass the agent_input dict (its ``history_scope`` is extracted) or
        ``history_scope`` directly; with neither, identity falls back to
        the bare pipeline surface.
        """
        from app.assistant.scope.loader import load_scope_for_source

        if history_scope is None and isinstance(agent_input, dict):
            history_scope = agent_input.get("history_scope")
        scope_data = history_scope if isinstance(history_scope, dict) else {}
        room_id = str(scope_data.get("room_id") or "").strip()
        room_surface = str(scope_data.get("room_surface") or "").strip()
        return load_scope_for_source(
            kind="pipeline",
            source_id="dayflow",
            actor_id=f"{self.step_id}_runner",
            identity_overrides={
                "room_id": room_id or None,
                "surface": (room_surface or None) or "pipeline",
            },
        )

    def _get_last_run_utc(self, ctx: StepContext):
        """This step's last_run_utc from pipeline state (aware UTC or None)."""
        from app.assistant.utils.time_utils import parse_iso_utc

        step_runs = ctx.state.get("step_runs", {})
        info = step_runs.get(self.step_id, {}) if isinstance(step_runs, dict) else {}
        return parse_iso_utc(info.get("last_run_utc")) if isinstance(info, dict) else None

    def _get_afk_snapshot(self, *, strict: bool = False) -> Dict[str, Any]:
        """Realtime AFKMonitor snapshot.

        ``strict=True`` raises loudly when the monitor is missing or returns
        a non-dict (steps whose output is meaningless without it); the
        default is best-effort and returns {}.
        """
        try:
            monitor = getattr(DI, "afk_monitor", None)
            if monitor is None:
                if strict:
                    logger.error("%s: DI.afk_monitor is missing (strict mode)", type(self).__name__)
                    raise RuntimeError("AFKMonitor missing: DI.afk_monitor is None")
                return {}
            snapshot = monitor.get_computer_activity()
            if not isinstance(snapshot, dict):
                if strict:
                    logger.error(
                        "%s: AFKMonitor returned invalid snapshot type=%s",
                        type(self).__name__, type(snapshot),
                    )
                    raise RuntimeError(f"AFKMonitor returned invalid snapshot type: {type(snapshot)}")
                return {}
            return snapshot
        except Exception:
            if strict:
                raise
            return {}

    def _boundary_date_local(self, ctx: StepContext) -> str:
        return str(ctx.state.get("boundary_date_local") or ctx.now_local.strftime("%Y-%m-%d"))

    def run(self, ctx: StepContext) -> StepResult:
        raise NotImplementedError

    def reset(self, ctx: StepContext) -> None:
        """
        Called when the daily boundary is crossed (or on cold start catch-up).
        Override to reset step-specific counters/resources.
        """

    def get_step_config(self) -> Dict[str, Any]:
        """
        Load this step's dedicated config file if it exists.

        Looks for: pipelines/dayflow/step_configs/config_step_<step_id>.json
        Returns empty dict if not found.
        """
        step_id = (getattr(self, "step_id", "") or "").strip()
        if not step_id:
            return {}
        config_path = Path(__file__).resolve().parent / "step_configs" / f"config_step_{step_id}.json"
        return _read_json_file(config_path) or {}

