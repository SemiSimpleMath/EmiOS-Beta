from __future__ import annotations

import re
from typing import Any, Dict, Tuple

from app.assistant.utils.pydantic_classes import Message, ToolResult
from app.assistant.utils.resource_formatting import format_resource_value
from app.assistant.utils.task_spec_loader import load_task_spec
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class RequestPreprocessor:
    """
    Normalize inbound manager requests before manager runtime execution.
    """

    def __init__(self, *, resource_manager: Any = None) -> None:
        self._resource_manager = resource_manager

    def _resolve_task_resources(
        self,
        *,
        manager_name: str,
        allowed_resources: list[str],
    ) -> tuple[dict[str, Any], str]:
        if not allowed_resources:
            return {}, ""

        resource_manager = self._resource_manager
        if resource_manager is None:
            from app.assistant.ServiceLocator.service_locator import DI  # local import — shim only
            resource_manager = getattr(DI, "resource_manager", None)
        if resource_manager is None:
            raise RuntimeError(f"[{manager_name}] resource_manager service is not registered.")

        resource_filter = {
            "resources": {
                "allowed_global_resources": list(allowed_resources),
                "allowed_room_resources": [],
                "denied_resources": [],
                "resource_groups": [],
            }
        }

        values = resource_manager.get_resources(
            scope_context=resource_filter,
            resource_ids=list(allowed_resources),
            required=False,
        )
        lines: list[str] = []
        for rid in allowed_resources:
            if rid not in values:
                continue
            rendered = format_resource_value(values.get(rid))
            if not rendered:
                continue
            lines.append(f"## {rid}\n{rendered}")
        return values, "\n\n".join(lines).strip()

    @staticmethod
    def _normalize_manager_name(manager_name: str) -> str:
        text = str(manager_name or "").strip()
        if not text:
            return ""
        # MultiAgentManager instances are often suffixed with "_<n>".
        return re.sub(r"_\d+$", "", text)

    @staticmethod
    def _read_caller_allowed_resources(user_message: Message) -> list[str]:
        scope = getattr(user_message, "scope_context", None)
        resources = None
        if hasattr(scope, "resources") and scope is not None:
            resources = getattr(scope.resources, "allowed_global_resources", None)
        elif isinstance(scope, dict):
            resources_block = scope.get("resources")
            if isinstance(resources_block, dict):
                resources = resources_block.get("allowed_global_resources")
        if resources is None:
            raise ValueError("scope_context.resources.allowed_global_resources is required for task resource resolution.")
        if not isinstance(resources, list):
            raise ValueError(
                "scope_context.resources.allowed_global_resources must be a list."
            )
        out: list[str] = []
        for item in resources:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out

    @staticmethod
    def _validate_string_list(value: Any, *, field_name: str) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} must be a list when provided, got {type(value).__name__}.")
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            else:
                raise ValueError(f"{field_name} entries must be non-empty strings.")
        return out

    def _resolve_effective_task_allowed_resources(
        self,
        *,
        manager_name: str,
        user_message: Message,
        frontmatter: dict[str, Any],
    ) -> tuple[list[str], dict[str, Any]]:
        canonical_manager = self._normalize_manager_name(manager_name)
        caller_allowed = self._read_caller_allowed_resources(user_message)
        caller_allow_all = len(caller_allowed) == 1 and caller_allowed[0] == "all"

        manager_resources = frontmatter.get("manager_resources")
        selection_source = "task.allowed_resources"
        selected: Any
        if isinstance(manager_resources, dict):
            if canonical_manager in manager_resources:
                selected = manager_resources.get(canonical_manager)
                selection_source = f"task.manager_resources.{canonical_manager}"
            elif "default" in manager_resources:
                selected = manager_resources.get("default")
                selection_source = "task.manager_resources.default"
            else:
                selected = frontmatter.get("allowed_resources")
        elif manager_resources is None:
            selected = frontmatter.get("allowed_resources")
        else:
            raise ValueError("task frontmatter 'manager_resources' must be an object when provided.")

        # None or omitted => inherit caller scope.
        if selected is None:
            return list(caller_allowed), {
                "mode": "inherit",
                "selection_source": selection_source,
                "caller_allowed_global_resources": list(caller_allowed),
                "effective_allowed_global_resources": list(caller_allowed),
            }

        explicit = self._validate_string_list(selected, field_name=selection_source)
        if len(explicit) == 0:
            return [], {
                "mode": "explicit_empty",
                "selection_source": selection_source,
                "caller_allowed_global_resources": list(caller_allowed),
                "effective_allowed_global_resources": [],
            }

        if caller_allow_all:
            return list(explicit), {
                "mode": "explicit",
                "selection_source": selection_source,
                "caller_allowed_global_resources": list(caller_allowed),
                "effective_allowed_global_resources": list(explicit),
            }

        caller_set = set(caller_allowed)
        disallowed = [rid for rid in explicit if rid not in caller_set]
        if disallowed:
            raise PermissionError(
                f"[{manager_name}] Task requested resources outside caller scope: {disallowed}"
            )
        return list(explicit), {
            "mode": "explicit",
            "selection_source": selection_source,
            "caller_allowed_global_resources": list(caller_allowed),
            "effective_allowed_global_resources": list(explicit),
        }

    def preprocess(self, manager_name: str, user_message: Message) -> Tuple[Message, ToolResult | None]:
        if not isinstance(user_message, Message):
            raise TypeError(f"[{manager_name}] Expected Message, got {type(user_message)}")

        resolved_task = user_message.task
        resolved_info = user_message.information
        resolved_data: Dict[str, Any] = user_message.data if isinstance(user_message.data, dict) else {}
        is_task_ir_step = isinstance(resolved_data.get("task_ir_step_id"), str) and bool(
            str(resolved_data.get("task_ir_step_id") or "").strip()
        )

        task_file = resolved_data.get("task_file")
        if is_task_ir_step and isinstance(task_file, str) and task_file.strip():
            raise ValueError(
                f"[{manager_name}] task_file is not allowed for TaskIR step requests. "
                f"step_id={resolved_data.get('task_ir_step_id')!r}"
            )
        if isinstance(task_file, str) and task_file.strip():
            spec = load_task_spec(task_file.strip())
            inbound_task = str(resolved_task or "").strip()
            # task_file is metadata/resource context only.
            # Never backfill task text from spec body; callers must provide explicit task text.
            if not inbound_task:
                step_id = resolved_data.get("task_ir_step_id")
                raise ValueError(
                    f"[{manager_name}] Missing non-empty task text for task_file request. "
                    f"step_id={step_id!r}"
                )
            resolved_task = inbound_task
            resolved_data = dict(resolved_data)
            effective_allowed_resources, resource_policy = self._resolve_effective_task_allowed_resources(
                manager_name=manager_name,
                user_message=user_message,
                frontmatter=spec.frontmatter if isinstance(spec.frontmatter, dict) else {},
            )

            llm_params = None
            try:
                raw = spec.frontmatter.get("llm_params") if isinstance(spec.frontmatter, dict) else None
                llm_params = raw if isinstance(raw, dict) else None
            except Exception:
                logger.debug("[%s] Failed to resolve llm_params from task spec frontmatter", manager_name, exc_info=True)
                llm_params = None

            task_allowed_tools = None
            task_except_tools = None
            try:
                tools_block = spec.frontmatter.get("tools") if isinstance(spec.frontmatter, dict) else None
                if isinstance(tools_block, dict):
                    a = tools_block.get("allowed_tools")
                    e = tools_block.get("except_tools")
                    if isinstance(a, list):
                        task_allowed_tools = [str(x).strip() for x in a if isinstance(x, str) and x.strip()]
                    if isinstance(e, list):
                        task_except_tools = [str(x).strip() for x in e if isinstance(x, str) and x.strip()]
            except Exception:
                logger.debug(
                    "[%s] Failed to resolve task-level tool restrictions from task spec",
                    manager_name,
                    exc_info=True,
                )
                task_allowed_tools = None
                task_except_tools = None

            resolved_data.update(
                {
                    "task_id": spec.task_id,
                    "task_description": spec.description,
                    "task_includes": spec.task_includes,
                    "allowed_resources": effective_allowed_resources,
                    "allowed_global_resources": effective_allowed_resources,
                    "allowed_read_files": spec.allowed_read_files,
                    "allowed_write_files": spec.allowed_write_files,
                    "task_spec": spec.frontmatter,
                    "task_resource_policy": resource_policy,
                    "task_llm_params": llm_params,
                    "task_allowed_tools": task_allowed_tools,
                    "task_except_tools": task_except_tools,
                }
            )
            task_resources, task_resource_context = self._resolve_task_resources(
                manager_name=manager_name,
                allowed_resources=effective_allowed_resources,
            )
            resolved_data["task_resources"] = task_resources
            resolved_data["task_resource_context"] = task_resource_context

        # Deterministic skip sentinel for spec-driven tasks.
        try:
            t = (resolved_task or "").strip()
            if t.upper().startswith("SKIPPED:"):
                skip = ToolResult(result_type="final_answer", content=t, data_list=[{"status": "skipped"}])
                return user_message.model_copy(
                    update={"task": resolved_task, "information": resolved_info, "data": resolved_data}
                ), skip
        except Exception:
            logger.debug("[%s] Failed while evaluating SKIPPED sentinel", manager_name, exc_info=True)

        normalized = user_message.model_copy(
            update={
                "task": resolved_task,
                "information": resolved_info,
                "data": resolved_data,
            }
        )
        return normalized, None

