from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.job_spec_loader import load_job_spec
from app.assistant.ServiceLocator.service_locator import DI

logger = get_logger(__name__)


class RunJobSpecTool(BaseTool):
    def __init__(self):
        super().__init__("run_job_spec")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = tool_message.tool_data.get("arguments", {}) if tool_message.tool_data else {}
        job_file = args.get("job_file")
        orchestrator_name = args.get("orchestrator") or "orchestrator_test"
        if not isinstance(job_file, str) or not job_file.strip():
            return ToolResult(result_type="error", content="Missing required argument: job_file")

        try:
            spec = load_job_spec(job_file.strip())
        except Exception as e:
            return ToolResult(result_type="error", content=f"Failed to load job spec: {e}")

        job_task_map: dict[str, dict] = {}
        for t in spec.tasks:
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
            "orchestrator_job_spec": spec.frontmatter,
            "orchestrator_job_bundle": spec.job_bundle_text,
            "orchestrator_job_task_map": job_task_map,
        }

        orch = DI.orchestrator_factory.create_orchestrator(str(orchestrator_name))
        info = ""
        if isinstance(spec.global_context, dict):
            info = str(spec.global_context.get("summary") or "")
        return orch.run(
            task=spec.job_bundle_text,
            information=info,
            data=data_payload,
        )


def get_tool_class():
    return RunJobSpecTool
