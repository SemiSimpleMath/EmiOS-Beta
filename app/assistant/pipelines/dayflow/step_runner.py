from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Tuple

from app.assistant.pipelines.audit_retention import prune_pipeline_runs_dir
from app.assistant.utils.logging_config import get_logger

from .context import DayFlowContext
from .step_types import StepContext, StepResult

logger = get_logger(__name__)


class Step(Protocol):
    step_id: str

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        ...

    def run(self, ctx: StepContext) -> StepResult:
        ...

    def reset(self, ctx: StepContext) -> None:
        ...

    def get_step_config(self) -> Dict[str, Any]:
        ...


@dataclass
class StepExecResult:
    name: str
    status: str  # success|skipped|error|reset
    duration_s: float
    reason: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "duration_s": round(float(self.duration_s), 2),
        }
        if self.reason:
            d["reason"] = self.reason
        if self.error:
            d["error"] = self.error
        return d


def _load_step_class(dotted: str):
    if ":" not in dotted:
        raise ValueError(f"Invalid step_class '{dotted}'. Expected 'module.path:ClassName'.")
    mod_path, cls_name = dotted.split(":", 1)
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name)

def _step_name(step: Step) -> str:
    sid = (getattr(step, "step_id", "") or "").strip()
    return sid or step.__class__.__name__


class DayFlowRunner:
    def __init__(self, *, steps: List[Step]):
        self.steps = steps

    def run_once(self, ctx: DayFlowContext, *, only_steps: Optional[list[str]] = None) -> Dict[str, Any]:
        ctx.day_dir.mkdir(parents=True, exist_ok=True)
        ctx.pipeline_runs_dir.mkdir(parents=True, exist_ok=True)

        wanted = set([s.strip() for s in (only_steps or []) if str(s).strip()]) if only_steps else None

        started_at = ctx.now_utc.isoformat()
        results: List[Dict[str, Any]] = []

        ctx.load_state()

        boundary_crossed = ctx.ensure_boundary_state()
        reset_ran = False
        if ctx.daily_reset_needed():
            t0 = time.monotonic()
            for step in self.steps:
                name = _step_name(step)
                if wanted is not None and name not in wanted:
                    continue
                try:
                    step_cfg = step.get_step_config() if hasattr(step, "get_step_config") else {}
                    step_ctx = StepContext(
                        now_utc=ctx.now_utc,
                        now_local=ctx.now_local,
                        state=ctx.state,
                        pipeline_config=ctx.pipeline_config,
                        step_config=step_cfg if isinstance(step_cfg, dict) else {},
                        resources_dir=ctx.resources_dir,
                        day_dir=ctx.day_dir,
                    )
                    step.reset(step_ctx)
                except Exception as e:
                    logger.error("[dayflow] reset failed for %s: %s", name, e)
            ctx.mark_daily_reset_done()
            reset_ran = True
            results.append(
                StepExecResult(
                    name="__daily_reset__",
                    status="success",
                    duration_s=time.monotonic() - t0,
                    reason=f"boundary={ctx.boundary_date_local}",
                ).to_dict()
            )

        for step in self.steps:
            name = _step_name(step)
            if wanted is not None and name not in wanted:
                continue

            step_cfg = step.get_step_config() if hasattr(step, "get_step_config") else {}
            step_ctx = StepContext(
                now_utc=ctx.now_utc,
                now_local=ctx.now_local,
                state=ctx.state,
                pipeline_config=ctx.pipeline_config,
                step_config=step_cfg if isinstance(step_cfg, dict) else {},
                resources_dir=ctx.resources_dir,
                day_dir=ctx.day_dir,
            )

            try:
                should, reason = step.should_run(step_ctx)
            except Exception as e:
                should, reason = True, f"gate_error:{str(e)}"
            if not should:
                results.append(StepExecResult(name=name, status="skipped", duration_s=0.0, reason=reason).to_dict())
                continue

            t0 = time.monotonic()
            try:
                logger.info("[dayflow_:%s] >> %s (%s)", ctx.run_id, name, reason)
                result = step.run(step_ctx)
                if isinstance(result, StepResult) and result.state_updates:
                    for k, v in (result.state_updates or {}).items():
                        ctx.state[k] = v

                runs = ctx.state.setdefault("step_runs", {})
                existing = runs.get(name)
                info: Dict[str, Any] = existing if isinstance(existing, dict) else {}
                info.update(
                    {
                        "last_run_utc": ctx.now_utc.isoformat(),
                        "last_reason": reason,
                        "last_debug": getattr(result, "debug", None) if isinstance(result, StepResult) else None,
                    }
                )
                runs[name] = info

                results.append(
                    StepExecResult(name=name, status="success", duration_s=time.monotonic() - t0, reason=reason).to_dict()
                )
                logger.info("[dayflow_:%s] << %s OK", ctx.run_id, name)
            except Exception as e:
                results.append(
                    StepExecResult(
                        name=name,
                        status="error",
                        duration_s=time.monotonic() - t0,
                        reason=reason,
                        error=str(e),
                    ).to_dict()
                )
                logger.error("[dayflow_:%s] << %s FAILED", ctx.run_id, name)
                logger.debug("[dayflow_:%s] << %s FAILED exception details", ctx.run_id, name, exc_info=True)
                break

        ctx.save_state()
        finished_at = datetime.now(ctx.now_utc.tzinfo).isoformat()

        audit = {
            "schema_version": 1,
            "pipeline_id": ctx.pipeline_id,
            "run_id": ctx.run_id,
            "boundary_date_local": ctx.boundary_date_local,
            "started_at_utc": started_at,
            "finished_at_utc": finished_at,
            "boundary_crossed": bool(boundary_crossed),
            "daily_reset_ran": bool(reset_ran),
            "only_steps": list(only_steps) if only_steps else None,
            "steps": results,
            "notes": "Step-based DayFlow.",
        }

        from app.assistant.utils.atomic_write import write_json_atomic

        write_json_atomic(ctx.audit_path(), audit)

        # Retain only the most recent audits for this day.
        try:
            prune_pipeline_runs_dir(pipeline_runs_dir=ctx.pipeline_runs_dir, max_files=1500)
        except Exception:
            pass
        overall_status = "success"
        for r in results:
            if isinstance(r, dict) and r.get("status") == "error":
                overall_status = "error"
                break
        return {
            "pipeline_id": ctx.pipeline_id,
            "run_id": ctx.run_id,
            "status": overall_status,
            "boundary_date_local": ctx.boundary_date_local,
            "audit_path": str(ctx.audit_path()),
            "steps": results,
        }

