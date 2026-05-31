from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.routine_manager.run_types import RoutineRunContext, RoutineRunResult
from app.assistant.routine_manager.runners.types import RoutineLike
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root, resolve_repo_path
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.task_spec_loader import TaskSpec, load_task_spec

logger = get_logger(__name__)


class TaskRoutineRunner:
    """
    v2 task runner.

    Execution is explicit by mode:
    - prose_manager: execute task spec body by manager invocation.
    - compiled_task: execute compiled workflow JSON via Task IR runner service.
    """

    def run(self, routine: RoutineLike, run_ctx: RoutineRunContext) -> RoutineRunResult:
        _ = run_ctx
        spec = routine.spec if isinstance(routine.spec, dict) else {}
        task_file = str(spec.get("task_file") or "").strip()
        if not task_file:
            raise ValueError("task runner requires spec.task_file")

        task_spec = load_task_spec(task_file)
        mode = str(spec.get("execution_mode") or "prose_manager").strip().lower()
        if mode == "compiled_task":
            return self._run_compiled_task_mode(spec=spec, task_file=task_file, task_spec=task_spec)
        if mode == "prose_manager":
            return self._run_prose_mode(spec=spec, task_file=task_file, task_spec=task_spec)
        raise ValueError(f"Unsupported task runner execution_mode: {mode}")

    def _run_prose_mode(self, *, spec: dict[str, Any], task_file: str, task_spec: TaskSpec) -> RoutineRunResult:
        rendered = str(task_spec.task_body or "").strip()
        if rendered.upper().startswith("SKIPPED:"):
            return RoutineRunResult(
                status="skipped",
                message=rendered,
                data={"task_file": task_file, "task_id": task_spec.task_id, "execution_mode": "prose_manager"},
            )

        manager_type = str(spec.get("manager") or task_spec.manager or "").strip()
        if not manager_type:
            raise ValueError("Task spec missing manager and no spec.manager override provided")

        manager = DI.multi_agent_manager_factory.create_manager(manager_type)
        self._apply_manager_limits(manager=manager, task_spec=task_spec)

        data = {
            "task_id": task_spec.task_id,
            "task_description": task_spec.description,
            "task_spec": task_spec.frontmatter,
            "task_includes": task_spec.task_includes,
            "allowed_resources": task_spec.allowed_resources,
            "allowed_read_files": task_spec.allowed_read_files,
            "allowed_write_files": task_spec.allowed_write_files,
            "task_llm_params": (
                task_spec.frontmatter.get("llm_params")
                if isinstance(task_spec.frontmatter, dict) and isinstance(task_spec.frontmatter.get("llm_params"), dict)
                else None
            ),
            "task_runner_version": "v2",
            "task_execution_mode": "prose_manager",
        }
        # Standard task scope (authority 98) — a routine running a task spec is
        # task execution. owner_id/surface preserved as the routine identity.
        scope_contract = load_scope_for_source(
            kind="subsystem",
            source_id="task",
            actor_id="routine_task_runner_v2",
            identity_overrides={
                "owner_id": "routine_manager",
                "surface": "routine",
                "scope_id": f"scope::routine::{task_spec.task_id}",
            },
        )
        data["scope_contract"] = scope_contract.model_dump()

        DI.manager_invoker.invoke(
            manager,
            Message(
                content=f"Run task spec: {task_spec.task_id}",
                task=task_spec.task_body,
                information=str(spec.get("information") or ""),
                data=data,
                scope_context=scope_contract,
            ),
        )
        return RoutineRunResult(
            status="success",
            data={
                "task_file": task_file,
                "manager": manager_type,
                "execution_mode": "prose_manager",
            },
        )

    def _run_compiled_task_mode(self, *, spec: dict[str, Any], task_file: str, task_spec: TaskSpec) -> RoutineRunResult:
        compiled_file = self._resolve_compiled_file(spec=spec, task_spec=task_spec)
        compiled_task = self._read_compiled_task(compiled_file)

        runner = getattr(DI, "task_ir_runner", None)
        if runner is None:
            raise RuntimeError("task_ir_runner service is not registered.")

        runner.ensure_event_subscription()
        run_state = runner.start_run(
            compiled_task=compiled_task,
            initial_context=(spec.get("initial_context") if isinstance(spec.get("initial_context"), dict) else {}),
        )

        return RoutineRunResult(
            status="success",
            data={
                "task_file": task_file,
                "execution_mode": "compiled_task",
                "compiled_file": compiled_file,
                "run_id": str(run_state.get("run_id") or ""),
                "run_status": str(run_state.get("status") or ""),
                "waiting_event_name": str(run_state.get("waiting_event_name") or "") or None,
            },
        )

    def _resolve_compiled_file(self, *, spec: dict[str, Any], task_spec: TaskSpec) -> str:
        explicit_path = str(spec.get("compiled_file") or spec.get("compiled_task_file") or "").strip()
        if explicit_path:
            return explicit_path

        task_file_raw = str(spec.get("task_file") or "").strip()
        if task_file_raw:
            repo_root = get_repo_root()
            task_file_path = Path(task_file_raw)
            if not task_file_path.is_absolute():
                task_file_path = (repo_root / task_file_path).resolve()
            deterministic = (task_file_path.parent / f"{str(task_spec.task_id).strip()}.json").resolve()
            if deterministic.exists():
                return deterministic.relative_to(repo_root).as_posix()

        outputs = task_spec.frontmatter.get("outputs") if isinstance(task_spec.frontmatter, dict) else None
        if not isinstance(outputs, list) or not outputs:
            if task_file_raw:
                return deterministic.relative_to(repo_root).as_posix()
            raise ValueError("compiled_task mode requires outputs[] in task spec or spec.compiled_file")

        chosen: str = ""
        for output in outputs:
            if not isinstance(output, dict):
                continue
            output_id = str(output.get("id") or "").strip().lower()
            output_path = str(output.get("path") or "").strip()
            output_format = str(output.get("format") or "").strip().lower()
            if not output_path:
                continue
            if output_id in {"compiled_task", "compiled_preview"}:
                chosen = output_path
                break
            if output_format == "json" and not chosen:
                chosen = output_path
        if not chosen:
            if task_file_raw:
                return deterministic.relative_to(repo_root).as_posix()
            raise ValueError("compiled_task mode could not resolve JSON compiled output path")
        return chosen

    def _read_compiled_task(self, compiled_file: str) -> dict[str, Any]:
        path = resolve_repo_path(compiled_file)
        if not path.exists():
            raise FileNotFoundError(f"Compiled task file not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Compiled task file must contain a JSON object")
        return data

    def _apply_manager_limits(self, *, manager: Any, task_spec: TaskSpec) -> None:
        limits = task_spec.frontmatter.get("limits", {}) if isinstance(task_spec.frontmatter, dict) else {}
        if not isinstance(limits, dict):
            raise ValueError("task frontmatter limits must be an object when provided")
        if limits.get("max_cycles") is None:
            return
        max_cycles = int(limits.get("max_cycles"))
        if max_cycles <= 0:
            raise ValueError("task frontmatter limits.max_cycles must be > 0")
        manager.manager_config["max_cycles"] = max_cycles
        logger.info("Task runner v2 applied manager max_cycles override: %s", max_cycles)
