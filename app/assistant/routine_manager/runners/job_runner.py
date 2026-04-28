from __future__ import annotations

from typing import Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.job_spec_loader import load_job_spec

from .types import RoutineLike
from app.assistant.routine_manager.run_types import RoutineRunContext, RoutineRunResult


class JobRoutineRunner:
    def run(self, routine: RoutineLike, run_ctx: RoutineRunContext) -> RoutineRunResult:
        # target_date currently unused for job specs.
        spec = routine.spec or {}
        job_file = str(spec.get("job_file") or "").strip()
        if not job_file:
            raise ValueError("job runner requires spec.job_file")

        job_spec = load_job_spec(job_file)
        orchestrator_name = str(spec.get("orchestrator") or "orchestrator_test").strip()

        job_task_map: dict[str, dict] = {}
        for t in job_spec.tasks:
            job_task_map[t.job_id] = {
                "task": t.task_spec.task_body,
                "information": t.information,
                "inputs": {
                    "task_id": t.task_spec.task_id,
                    "task_description": t.task_spec.description,
                    "task_includes": t.task_spec.task_includes,
                    "allowed_resources": t.task_spec.allowed_resources,
                    "allowed_read_files": t.task_spec.allowed_read_files,
                    "allowed_write_files": t.task_spec.allowed_write_files,
                    "task_spec": t.task_spec.frontmatter,
                },
                "depends_on": t.depends_on,
                "success_criteria": t.success_criteria,
                "budget": t.budget,
                "manager": t.manager,
                "task_file": t.task_file,
            }

        data_payload = {
            "orchestrator_job_spec": job_spec.frontmatter,
            "orchestrator_job_bundle": job_spec.job_bundle_text,
            "orchestrator_job_task_map": job_task_map,
        }
        orch = DI.orchestrator_factory.create_orchestrator(orchestrator_name)
        info = ""
        if isinstance(job_spec.global_context, dict):
            info = str(job_spec.global_context.get("summary") or "")
        orch.run(
            task=job_spec.job_bundle_text,
            information=info,
            data=data_payload,
        )
        return RoutineRunResult(status="success", data={"job_file": job_file, "orchestrator": orchestrator_name})
