"""
Execution Trace Recorder — captures structured traces of multi-agent manager runs.

Non-blocking, fail-safe. Writes incrementally to a JSON artifact file
as events happen. SQLite summary written once at end_run().
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_ARTIFACT_DIR = "data/execution_traces"
DEFAULT_MAX_TRACES_PER_TASK = 50


class ExecutionTraceRecorder:
    """Captures execution trace events incrementally for one manager run."""

    def __init__(self, *, manager_name: str, config: Dict[str, Any] | None = None):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self._persist_json = bool(cfg.get("persist_json", True))
        self._persist_sqlite = bool(cfg.get("persist_sqlite", True))
        self._artifact_dir = str(cfg.get("artifact_dir", DEFAULT_ARTIFACT_DIR))
        self._max_traces = int(cfg.get("max_traces_per_task", DEFAULT_MAX_TRACES_PER_TASK))

        self.manager_name = manager_name
        self.run_id = str(uuid.uuid4())
        self.task_id: str = ""
        self.task_text: str = ""
        self.information: str = ""
        self.start_time: datetime = datetime.now(timezone.utc)
        self.end_time: Optional[datetime] = None
        self.exit_reason: str = ""
        self.success: bool = False
        self.final_answer: str = ""

        self.steps: List[Dict[str, Any]] = []
        self.critic_interventions: List[Dict[str, Any]] = []
        self.metadata: Dict[str, Any] = {}

        self._current_step: int = 0
        self._artifact_path: Optional[Path] = None

    # ------------------------------------------------------------------
    # Recording methods
    # ------------------------------------------------------------------

    def begin_run(self, *, task_id: str = "", task_text: str = "", information: str = "", **extra) -> None:
        if not self.enabled:
            return
        self.task_id = task_id
        self.task_text = task_text
        self.information = information
        self.start_time = datetime.now(timezone.utc)
        self.metadata.update(extra)

        # Create the artifact file immediately so partial traces survive crashes.
        if self._persist_json:
            self._init_artifact_file()

        logger.info("[trace] begin_run: manager=%s task_id=%s run_id=%s", self.manager_name, task_id, self.run_id)

    def record_step(
        self,
        *,
        step_number: int,
        agent_name: str,
        action: str = "",
        action_input: str = "",
        tool_arguments: Dict[str, Any] | None = None,
        page_url: str = "",
        reasoning: str = "",
        note: str = "",
    ) -> None:
        if not self.enabled:
            return
        self._current_step = step_number
        step = {
            "step_number": step_number,
            "agent_name": agent_name,
            "action": action,
            "action_input": action_input,
            "tool_arguments": tool_arguments,
            "page_url": page_url,
            "reasoning": reasoning,
            "note": note,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": None,
            "tool_result_type": None,
            "tool_result_preview": None,
            "tool_timing_ms": None,
            "selectors_used": None,
        }
        self.steps.append(step)
        self._flush_json()

    def record_tool_result(
        self,
        *,
        tool_name: str,
        result_type: str = "",
        content_preview: str = "",
        timing_ms: float | None = None,
        page_url: str = "",
        selectors_used: List[str] | None = None,
    ) -> None:
        """Enrich the most recent step with tool execution results."""
        if not self.enabled or not self.steps:
            return
        step = self.steps[-1]
        step["tool_name"] = tool_name
        step["tool_result_type"] = result_type
        step["tool_result_preview"] = content_preview[:500] if content_preview else ""
        step["tool_timing_ms"] = timing_ms
        if page_url:
            step["page_url"] = page_url
        if selectors_used:
            step["selectors_used"] = selectors_used
        self._flush_json()

    def record_critic_intervention(
        self,
        *,
        step_number: int = 0,
        must_revise: bool = False,
        diagnosis: str = "",
        actionable_change: str = "",
        confidence: float = 0.0,
        trigger_reason: str = "",
        planned_tool_name: str = "",
    ) -> None:
        if not self.enabled:
            return
        intervention = {
            "step_number": step_number or self._current_step,
            "must_revise": must_revise,
            "diagnosis": diagnosis,
            "actionable_change": actionable_change,
            "confidence": confidence,
            "trigger_reason": trigger_reason,
            "planned_tool_name": planned_tool_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.critic_interventions.append(intervention)
        self._flush_json()

    def end_run(self, *, exit_reason: str = "", success: bool = False, final_answer: str = "") -> None:
        if not self.enabled:
            return
        if self.end_time is not None:
            return  # Already finalized.
        self.end_time = datetime.now(timezone.utc)
        self.exit_reason = exit_reason
        self.success = success
        self.final_answer = final_answer[:1000] if final_answer else ""
        logger.info(
            "[trace] end_run: run_id=%s steps=%d critics=%d success=%s reason=%s",
            self.run_id, len(self.steps), len(self.critic_interventions), success, exit_reason,
        )
        # Final flush with complete metadata.
        self._flush_json()
        if self._persist_sqlite:
            self._write_sqlite()
        if self._persist_json:
            self._prune_old_traces()

    # ------------------------------------------------------------------
    # Incremental JSON persistence
    # ------------------------------------------------------------------

    def _init_artifact_file(self) -> None:
        """Create the artifact directory and file path."""
        try:
            task_dir = Path(self._artifact_dir) / (self.task_id or "_unnamed")
            task_dir.mkdir(parents=True, exist_ok=True)
            self._artifact_path = task_dir / f"{self.run_id}.json"
        except Exception as e:
            logger.error("[trace] failed to init artifact path: %s", e, exc_info=True)

    def _flush_json(self) -> None:
        """Write the current trace state to the JSON artifact file."""
        if not self._persist_json or not self._artifact_path:
            return
        try:
            doc = self._build_trace_document()
            self._artifact_path.write_text(
                json.dumps(doc, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug("[trace] flush_json failed: %s", e, exc_info=True)

    def _build_trace_document(self) -> Dict[str, Any]:
        return {
            "schema_version": "execution_trace_v1",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "spec_version": self.metadata.get("spec_version"),
            "manager_name": self.manager_name,
            "task_text": self.task_text,
            "information": self.information,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": (self.end_time - self.start_time).total_seconds() if self.end_time else None,
            "exit_reason": self.exit_reason,
            "success": self.success,
            "total_steps": len(self.steps),
            "total_critic_interventions": len(self.critic_interventions),
            "total_replans": sum(1 for c in self.critic_interventions if c.get("must_revise")),
            "final_answer": self.final_answer,
            "steps": self.steps,
            "critic_interventions": self.critic_interventions,
            "metadata": self.metadata,
        }

    # ------------------------------------------------------------------
    # SQLite (once at end_run)
    # ------------------------------------------------------------------

    def _write_sqlite(self) -> None:
        try:
            from app.models.base import get_session
            from app.assistant.execution_trace.execution_trace_models import ExecutionTraceRun, ExecutionTraceStep

            session = get_session()
            try:
                run_row = ExecutionTraceRun(
                    run_id=self.run_id,
                    task_id=self.task_id or None,
                    manager_name=self.manager_name,
                    task_text=self.task_text[:500] if self.task_text else None,
                    start_time=self.start_time,
                    end_time=self.end_time,
                    exit_reason=self.exit_reason or None,
                    success=self.success,
                    total_steps=len(self.steps),
                    total_critic_interventions=len(self.critic_interventions),
                    total_replans=sum(1 for c in self.critic_interventions if c.get("must_revise")),
                    final_answer_preview=self.final_answer[:200] if self.final_answer else None,
                    trace_artifact_path=str(self._artifact_path) if self._artifact_path else None,
                    metadata_json=self.metadata or None,
                )
                session.add(run_row)

                for step in self.steps:
                    step_row = ExecutionTraceStep(
                        run_id=self.run_id,
                        step_number=step.get("step_number", 0),
                        agent_name=step.get("agent_name"),
                        action=step.get("action"),
                        action_input_preview=str(step.get("action_input") or "")[:300],
                        tool_name=step.get("tool_name"),
                        tool_result_type=step.get("tool_result_type"),
                        tool_result_preview=step.get("tool_result_preview"),
                        tool_timing_ms=step.get("tool_timing_ms"),
                        page_url=step.get("page_url"),
                        selectors_used_json=step.get("selectors_used"),
                        timestamp=datetime.fromisoformat(step["timestamp"]) if step.get("timestamp") else None,
                    )
                    session.add(step_row)

                session.commit()
                logger.info("[trace] wrote %d step rows to SQLite for run %s", len(self.steps), self.run_id)
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        except Exception as e:
            logger.error("[trace] failed writing to SQLite: %s", e, exc_info=True)

    def _prune_old_traces(self) -> None:
        try:
            task_dir = Path(self._artifact_dir) / (self.task_id or "_unnamed")
            if not task_dir.exists():
                return
            traces = sorted(task_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            while len(traces) > self._max_traces:
                oldest = traces.pop(0)
                oldest.unlink()
                logger.debug("[trace] pruned old trace: %s", oldest.name)
        except Exception as e:
            logger.debug("[trace] prune failed: %s", e)
