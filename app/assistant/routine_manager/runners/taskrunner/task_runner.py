from __future__ import annotations

from pathlib import Path
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.routine_manager.run_types import RoutineRunContext, RoutineRunResult
from app.assistant.routine_manager.runners.types import RoutineLike
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.task_spec_loader import TaskSpec, load_task_spec, read_compiled_task


class TaskRoutineRunner:
    """
    v2 task runner.

    Executes a compiled workflow JSON via the Task IR runner service. The task
    inherits a standard task-execution scope (it is a scope consumer, not a
    source), so it never falls back to the IR executor's system scope.
    """

    def run(self, routine: RoutineLike, run_ctx: RoutineRunContext) -> RoutineRunResult:
        _ = run_ctx
        spec = routine.spec if isinstance(routine.spec, dict) else {}
        task_file = str(spec.get("task_file") or "").strip()
        if not task_file:
            raise ValueError("task runner requires spec.task_file")

        task_spec = load_task_spec(task_file)
        mode = str(spec.get("execution_mode") or "compiled_task").strip().lower()
        if mode != "compiled_task":
            raise ValueError(
                f"Unsupported task runner execution_mode: {mode!r}. "
                "Tasks are always compiled; the prose_manager path was removed."
            )
        return self._run_compiled_task_mode(spec=spec, task_file=task_file, task_spec=task_spec)

    def _run_compiled_task_mode(self, *, spec: dict[str, Any], task_file: str, task_spec: TaskSpec) -> RoutineRunResult:
        compiled_file = self._resolve_compiled_file(spec=spec, task_spec=task_spec)
        compiled_task = read_compiled_task(compiled_file)

        # A work-object template (Option-B compiler output) drives on the task runner, not task-IR.
        # Detected by driver=task_runner + a nodes list; task_ir_v1 files fall through below (coexistence
        # until the archive cutover).
        if isinstance(compiled_task, dict) and compiled_task.get("driver") == "task_runner" \
                and isinstance(compiled_task.get("nodes"), list):
            from app.assistant.task_runtime.entry import start_task_run
            result = start_task_run(compiled_task)
            return RoutineRunResult(status="success", data={
                "task_file": task_file, "execution_mode": "work_object", "compiled_file": compiled_file,
                "work_id": str(result.get("work_id") or ""), "run_status": str(result.get("status") or "")})

        runner = getattr(DI, "task_ir_runner", None)
        if runner is None:
            raise RuntimeError("task_ir_runner service is not registered.")

        # A task is a scope CONSUMER, not a source: it inherits the standard
        # task-execution scope (authority 98) built at the execution entry — the
        # same scope the run_task tool threads from its caller. Without this the
        # IR executor falls to its fail-closed system scope (authority 95,
        # resources zeroed). owner_id/surface carry the routine identity.
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
        initial_context = (
            dict(spec.get("initial_context")) if isinstance(spec.get("initial_context"), dict) else {}
        )
        initial_context.setdefault("_task_ir_inherited_scope_context", scope_contract.model_dump())

        runner.ensure_event_subscription()
        run_state = runner.start_run(
            compiled_task=compiled_task,
            initial_context=initial_context,
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
