from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Edit these two fields, then run this file as a module.
# ---------------------------------------------------------------------------
TASK_NAME = "my_timesheets"
TASK_DESCRIPTION = """
1) Read the dates in `tasks/timesheet/timesheet_2026_2_20.txt` marked like `*M/D/YY*`.
2) For each date marker, read the text between this marker and the next marker (or EOF for last one).
3) For each date, write narratives per company in this format:
   "<date> <entity>: <time entry block>"
4) If notes are unclear, write:
   "<date> <entity>: [Notes are lacking human assistance needed]"
"""

# Optional behavior toggles.
REGISTER_IN_TASK_REGISTRY = True
RUN_COMPILE_NOW = True


def _repo_root() -> Path:
    return get_repo_root()


def _slugify_task_name(raw: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", str(raw or "").strip().lower()).strip("_")
    if not value:
        raise ValueError("TASK_NAME must contain at least one alphanumeric character.")
    return value


def _task_spec_path(task_id: str) -> Path:
    return (_repo_root() / "tasks" / task_id / "task_spec.md").resolve()


def _compiled_output_path(task_id: str) -> str:
    return f"tasks/{task_id}/{task_id}.json"


def _build_task_spec_markdown(*, task_id: str, task_name: str, task_description: str) -> str:
    description_clean = str(task_description or "").strip()
    if not description_clean:
        raise ValueError("TASK_DESCRIPTION must be non-empty.")
    output_path = _compiled_output_path(task_id)
    frontmatter = {
        "schema_version": 1,
        "task_id": task_id,
        "manager": "emi_team_manager",
        "description": f"Compiled task for {task_name}.",
        "inputs": [],
        "outputs": [
            {
                "id": "compiled_preview",
                "path": output_path,
                "format": "json",
                "overwrite": True,
            }
        ],
        "idempotency": "overwrite_outputs",
        "limits": {"timeout_seconds": 300, "max_cycles": 30},
    }
    yaml_frontmatter = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=False).strip()
    return f"---\n{yaml_frontmatter}\n---\n# Task\n\n{description_clean}\n"


def _extract_compiled_task_json(result_data: dict[str, Any]) -> str:
    data_list = result_data.get("final_answer_data_list", [])
    if not isinstance(data_list, list):
        raise TypeError("final_answer_data_list must be list.")
    for item in data_list:
        if isinstance(item, dict) and item.get("key") == "compiled_task":
            raw = item.get("value")
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("compiled_task value missing from final_answer_data_list.")
            return raw
    raise ValueError("compiled_task item missing from final_answer_data_list.")


def _register_task(task_id: str, task_file_rel: str) -> None:
    registry_path = (_repo_root() / "tasks" / "registry.yaml").resolve()
    if not registry_path.exists():
        raise FileNotFoundError(f"Task registry file not found: {registry_path}")
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("tasks/registry.yaml must be a YAML object.")
    tasks = data.get("tasks")
    if tasks is None:
        tasks = []
    if not isinstance(tasks, list):
        raise ValueError("tasks/registry.yaml key 'tasks' must be a list.")

    for item in tasks:
        if isinstance(item, dict) and str(item.get("id") or "").strip() == task_id:
            logger.info("Task id already exists in registry; skipping add: %s", task_id)
            return

    tasks.append(
        {
            "id": task_id,
            "name": task_id.replace("_", " ").strip(),
            "task_file": task_file_rel,
            "description": f"Compiled task generated from editable string for '{task_id}'.",
            "aliases": [task_id, task_id.replace("_", " ")],
        }
    )
    data["tasks"] = tasks
    registry_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")
    logger.info("Added task to registry: %s", task_id)


def main() -> None:
    task_name_raw = str(TASK_NAME or "").strip()
    if not task_name_raw:
        raise ValueError("TASK_NAME must be non-empty.")
    task_id = _slugify_task_name(task_name_raw)
    task_description = str(TASK_DESCRIPTION or "").strip()
    task_spec_path = _task_spec_path(task_id)
    task_spec_path.parent.mkdir(parents=True, exist_ok=True)

    spec_markdown = _build_task_spec_markdown(
        task_id=task_id,
        task_name=task_name_raw,
        task_description=task_description,
    )
    task_spec_path.write_text(spec_markdown, encoding="utf-8")
    rel_task_spec = task_spec_path.relative_to(_repo_root()).as_posix()
    logger.info("Wrote task spec: %s", rel_task_spec)

    if REGISTER_IN_TASK_REGISTRY:
        _register_task(task_id, rel_task_spec)

    if not RUN_COMPILE_NOW:
        logger.info("RUN_COMPILE_NOW=false; done after writing task spec.")
        return

    DI.manager_registry.preload_all()
    manager = DI.multi_agent_manager_factory.create_manager("task_compile_manager")
    # Attach a system scope so the manager runs with the authority/allow/block
    # gate ENFORCED. Without a scope_context, MultiAgentManager sets
    # scope_contract_enforced=False and runs the manager fully ungated (scope
    # audit, 2026-06-13). authority 99 = the autonomous-subsystem default (task
    # compilation is internal, acting on its own behalf); it preserves the tool
    # access this manager had when ungated while turning the owner/allow gate on,
    # so it enforces the contract without regressing compilation.
    from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
    scope = build_system_scope_context(
        owner_id="system", actor_id="task_compiler", surface="system",
        authority_level=99,
    )
    request = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content="",
        task=task_description,
        information=json.dumps(
            {
                "task_id": task_id,
                "task_file": rel_task_spec,
                "task_description": f"Compiled task for {task_name_raw}.",
            },
            ensure_ascii=False,
        ),
        scope_context=scope,
    )
    result = manager.request_handler(request)
    result_data = result.data if isinstance(result.data, dict) else {}
    compiled_raw = _extract_compiled_task_json(result_data)
    compiled_obj = json.loads(compiled_raw)

    out_rel = _compiled_output_path(task_id)
    out_path = (_repo_root() / out_rel).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(compiled_obj, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote compiled output: %s", out_rel)
    print(f"TASK_ID: {task_id}")
    print(f"TASK_SPEC: {rel_task_spec}")
    print(f"COMPILED_OUTPUT: {out_rel}")


if __name__ == "__main__":
    main()
