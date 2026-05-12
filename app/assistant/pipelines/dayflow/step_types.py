from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.ServiceLocator.service_locator import DI
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


def _write_json_file_atomic(path: Path, data: Dict[str, Any]) -> None:
    """
    Atomic JSON write (best-effort) to reduce corruption risk on crash.
    """
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
        _write_json_file_atomic(self.resources_dir / filename, data)

        # 2) Archive mirror (best-effort)
        try:
            if filename.startswith("resource_") and filename.lower().endswith(".json"):
                snap_dir = self.day_dir / "resource_snapshots"
                _write_json_file_atomic(snap_dir / filename, data)
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


class BaseStep:
    """
    Base class for DayFlow steps.
    """

    step_id: str = ""

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        return True, "ready"

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

