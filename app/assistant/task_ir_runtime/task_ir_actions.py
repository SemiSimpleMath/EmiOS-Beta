from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.manager_interface.manager_interface import ManagerInterface
from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
from app.assistant.task_ir_runtime.task_ir_steps import TaskIRSteps
from app.assistant.task_ir_runtime.task_ir_validator import TaskIRValidator
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)
_MISSING = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _value_preview(value: Any, *, max_chars: int = 800) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except Exception:
        logger.debug("TaskIRActionExecutor value preview JSON encoding failed", exc_info=True)
        text = str(value)
    text = text.replace("\n", " ").strip()
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    return text


class TaskIRActionExecutor:
    def __init__(
        self,
        *,
        normalize_context: Callable[[dict[str, Any]], dict[str, Any]],
        apply_task_state_update: Callable[[dict[str, Any], dict[str, Any]], None],
        append_task_log: Callable[[dict[str, Any], dict[str, Any]], None],
        apply_step_task_state_update: Callable[[dict[str, Any], dict[str, Any]], None],
    ) -> None:
        self._normalize_context = normalize_context
        self._apply_task_state_update = apply_task_state_update
        self._append_task_log = append_task_log
        self._apply_step_task_state_update = apply_step_task_state_update
        self._validator = TaskIRValidator()

    def execute_action_step(
        self,
        *,
        step: dict[str, Any],
        context: dict[str, Any],
        task: dict[str, Any],
    ) -> dict[str, Any]:
        context = self._normalize_context(context)
        executor = str(step.get("executor") or "").strip()
        instruction = str(step.get("instruction") or "").strip()
        title = str(step.get("title") or "").strip()
        step_id = str(step.get("id") or "").strip()
        if not executor:
            raise ValueError(f"Action step missing executor: {step.get('id')}")
        if not instruction:
            raise ValueError(f"Action step missing instruction: {step.get('id')}")
        logger.info(
            "TASKIR ACTION START step_id=%s executor=%s title=%s",
            step_id,
            executor,
            title or step_id,
        )
        consumed_inputs = self.resolve_consumed_inputs(step=step, context=context, task=task)
        preloaded_inputs = self.resolve_preloaded_inputs(task=task, context=context)
        produced_ids = TaskIRSteps.normalized_step_data_ids(step=step, field_name="produces_data_ids")
        information_lines = [f"TaskIR step '{title or step.get('id')}'"]
        if consumed_inputs:
            from app.assistant.utils.prompt_formatter import format_for_prompt
            for i, (data_id, value) in enumerate(consumed_inputs.items(), 1):
                label = data_id
                for binding in (task.get("data_bindings") or []):
                    if isinstance(binding, dict) and binding.get("data_id") == data_id:
                        desc = binding.get("description", "")
                        if desc:
                            label = f"{data_id} — {desc}"
                        break
                formatted = format_for_prompt(value) if isinstance(value, (dict, list)) else str(value)
                information_lines.append(f"Input {i} ({label}):\n{formatted}")
        if preloaded_inputs:
            from app.assistant.utils.prompt_formatter import format_for_prompt
            for i, (data_id, value) in enumerate(preloaded_inputs.items(), 1):
                formatted = format_for_prompt(value) if isinstance(value, (dict, list)) else str(value)
                information_lines.append(f"Preloaded ({data_id}):\n{formatted}")
        step_information = "\n\n".join(information_lines)
        manager_scope_context = self.resolve_task_ir_step_scope_context(context=context, executor=executor)
        task_llm_params = self.resolve_task_llm_params_for_step(task=task, step=step)
        output_schema = self._validator.resolve_step_output_schema(step=step)
        # consumed/preloaded values are passed via information (natural language context for manager).
        # pinned_tools on a step overrides tool visibility — only those tools will be shown to the planner.
        data_payload = {
            "task_ir_step_id": step.get("id"),
            "task_ir_step_title": title,
            "task_ir_output_target_data_ids": produced_ids,
        }
        pinned_tools = step.get("pinned_tools")
        if isinstance(pinned_tools, list) and pinned_tools:
            data_payload["visible_tools"] = [str(t).strip() for t in pinned_tools if isinstance(t, str) and str(t).strip()]
        if isinstance(task_llm_params, dict) and task_llm_params:
            data_payload["task_llm_params"] = task_llm_params
        if isinstance(output_schema, dict):
            data_payload["task_ir_output_schema"] = output_schema
        interface = ManagerInterface(executor)
        tool_message = ToolMessage(
            tool_name=executor,
            tool_data={
                "tool_name": executor,
                "arguments": {"task": instruction, "information": step_information},
                "data": data_payload,
            },
            scope_context=manager_scope_context,
        )
        result = interface.execute(tool_message)
        if not isinstance(result, ToolResult):
            raise RuntimeError(f"Manager returned non-ToolResult for executor={executor}")
        result_type = str(result.result_type or "").strip().lower()
        if result_type in {"error", "tool_error", "manager_aborted"}:
            raise RuntimeError(f"Action step failed executor={executor}: {result.content}")
        logger.info("TASKIR ACTION DONE step_id=%s executor=%s result_type=%s", step_id, executor, result_type)
        history = context.get("action_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "step_id": str(step.get("id") or ""),
                "executor": executor,
                "result_type": result_type,
                "content": str(result.content or ""),
                "timestamp_utc": _now_iso(),
            }
        )
        context["action_history"] = history
        context["last_action_result"] = {
            "step_id": str(step.get("id") or ""),
            "executor": executor,
            "result_type": result_type,
            "content": str(result.content or ""),
            "data": result.data if isinstance(result.data, dict) else {},
        }
        task_state = context["task_state"]
        artifacts_obj = task_state["artifacts"]
        action_results = artifacts_obj.get("action_results")
        if not isinstance(action_results, dict):
            action_results = {}
        action_results[step_id] = dict(context["last_action_result"])
        artifacts_obj["action_results"] = action_results
        state_update = self.normalize_manager_result_to_task_state_update(step=step, result=result)
        if state_update is not None:
            self._apply_task_state_update(context, state_update)
        self._apply_step_task_state_update(step, context)
        self.enforce_declared_outputs_materialized(step=step, context=context, task=task)
        self.log_action_outputs(step=step, context=context)
        self._append_task_log(
            context,
            {
                "event_type": "action_executed",
                "step_id": step_id,
                "executor": executor,
                "result_type": result_type,
                "timestamp_utc": _now_iso(),
            },
        )
        return context

    def normalize_manager_result_to_task_state_update(
        self,
        *,
        step: dict[str, Any],
        result: ToolResult,
    ) -> Optional[dict[str, Any]]:
        step_id = str(step.get("id") or "").strip()
        produced_ids = TaskIRSteps.normalized_step_data_ids(step=step, field_name="produces_data_ids")
        result_data = result.data if isinstance(result.data, dict) else {}
        explicit_update = result_data.get("task_state_update")
        if explicit_update is not None:
            if not isinstance(explicit_update, dict):
                raise ValueError("task_state_update must be an object when provided by action result data.")
            return explicit_update
        if not produced_ids:
            return None
        normalized_update: dict[str, Any] = {"facts": {}, "artifacts": {}, "flags": {}}
        data_list_map = self.extract_final_answer_data_map(result_data)
        unresolved: list[str] = []
        for data_id in produced_ids:
            mapped = data_list_map.get(data_id, _MISSING)
            if mapped is _MISSING:
                unresolved.append(data_id)
                continue
            if data_id.startswith("fact_"):
                normalized_update["facts"][data_id] = mapped
            elif data_id.startswith("artifact_"):
                normalized_update["artifacts"][data_id] = mapped
            else:
                raise ValueError(f"Unsupported produced data id prefix for '{data_id}'.")
        if unresolved:
            if len(produced_ids) == 1 and len(unresolved) == 1:
                target_id = unresolved[0]
                inferred = self._default_materialize_single_output(
                    step=step,
                    result=result,
                )
                if target_id.startswith("fact_"):
                    normalized_update["facts"][target_id] = inferred
                elif target_id.startswith("artifact_"):
                    normalized_update["artifacts"][target_id] = self._coerce_artifact_runtime_value(inferred)
                else:
                    raise ValueError(f"Unsupported produced data id prefix for '{target_id}'.")
                unresolved = []
                logger.info(
                    "TaskIRActionExecutor materialized single produced id '%s' from manager output envelope.",
                    target_id,
                )
        if unresolved:
            raise ValueError(
                f"Step '{step_id}' produced ids {produced_ids} but manager output could not materialize ids: {unresolved}. "
                "Provide task_state_update explicitly or include matching final_answer_data_list keys."
            )
        self.enforce_step_output_schema(step=step, normalized_update=normalized_update, produced_ids=produced_ids)
        return normalized_update

    def enforce_step_output_schema(
        self,
        *,
        step: dict[str, Any],
        normalized_update: dict[str, Any],
        produced_ids: list[str],
    ) -> None:
        schema = self._validator.resolve_step_output_schema(step=step)
        if not isinstance(schema, dict):
            return
        step_id = str(step.get("id") or "").strip()
        if len(produced_ids) != 1:
            raise ValueError(
                f"Step '{step_id}' defines output_schema but produces_data_ids count is {len(produced_ids)}; expected exactly 1."
            )
        target_data_id = produced_ids[0]
        bucket_name = "facts" if target_data_id.startswith("fact_") else "artifacts"
        bucket = normalized_update.get(bucket_name)
        if not isinstance(bucket, dict):
            raise ValueError(f"Step '{step_id}' normalized update bucket '{bucket_name}' is missing.")
        if target_data_id not in bucket:
            raise ValueError(f"Step '{step_id}' output_schema target '{target_data_id}' missing from manager update.")
        self._validator.validate_output_value_against_schema(
            step_id=step_id,
            data_id=target_data_id,
            schema=schema,
            value=bucket[target_data_id],
        )

    def extract_final_answer_data_map(self, result_data: dict[str, Any]) -> dict[str, Any]:
        data_list = result_data.get("final_answer_data_list")
        if not isinstance(data_list, list):
            return {}
        out: dict[str, Any] = {}
        for item in data_list:
            if not isinstance(item, dict):
                raise ValueError("final_answer_data_list entries must be objects.")
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            raw_value = item.get("value")
            out[key] = self.try_parse_json_string(raw_value) if isinstance(raw_value, str) else raw_value
        return out

    @staticmethod
    def try_parse_json_string(raw: str) -> Any:
        value = str(raw or "").strip()
        if not value:
            return ""
        # Plain strings are common in final_answer_data_list.value and are valid.
        # Only attempt JSON parsing when the payload looks like JSON or JSON literals.
        lower_value = value.lower()
        if not (
            value.startswith("{")
            or value.startswith("[")
            or value.startswith('"')
            or lower_value in {"true", "false", "null"}
            or lower_value.replace(".", "", 1).replace("-", "", 1).isdigit()
        ):
            return value
        try:
            return json.loads(value)
        except Exception:
            logger.debug("TaskIRActionExecutor JSON parse failed for final_answer_data_list value", exc_info=True)
            return value

    @staticmethod
    def _default_materialize_single_output(*, step: dict[str, Any], result: ToolResult) -> Any:
        _ = step
        # Deterministic rule: produced single output gets the manager output envelope.
        if result.data is not None:
            return result.data
        if isinstance(result.data_list, list) and result.data_list:
            return list(result.data_list)
        return str(result.content or "")

    @staticmethod
    def _coerce_artifact_runtime_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            logger.debug("TaskIRActionExecutor artifact json serialization failed", exc_info=True)
            return str(value)

    def enforce_declared_outputs_materialized(
        self,
        *,
        step: dict[str, Any],
        context: dict[str, Any],
        task: dict[str, Any],
    ) -> None:
        produced_ids = TaskIRSteps.normalized_step_data_ids(step=step, field_name="produces_data_ids")
        if not produced_ids:
            return
        step_id = str(step.get("id") or "").strip()
        task_state = context["task_state"]
        data_index = task_state["data_index"]
        for data_id in produced_ids:
            path = self.resolve_state_path_for_data_id(task=task, data_id=data_id)
            value = self.get_by_dot_path(context, path, default=_MISSING)
            if value is _MISSING:
                raise ValueError(
                    f"Step '{step_id}' declares produced data id '{data_id}' at '{path}', but value was not materialized."
                )
            data_index[data_id] = {"state_path": path, "producer_step_id": step_id, "produced_at_utc": _now_iso()}

    def resolve_consumed_inputs(
        self,
        *,
        step: dict[str, Any],
        context: dict[str, Any],
        task: dict[str, Any],
    ) -> dict[str, Any]:
        consumed_ids = TaskIRSteps.normalized_step_data_ids(step=step, field_name="consumes_data_ids")
        if not consumed_ids:
            return {}
        resolved: dict[str, Any] = {}
        for data_id in consumed_ids:
            path = self.resolve_state_path_for_data_id(task=task, data_id=data_id)
            value = self.get_by_dot_path(context, path, default=_MISSING)
            if value is _MISSING:
                raise ValueError(f"Step '{step.get('id')}' consumes data id '{data_id}' at '{path}', but value is missing.")
            resolved[data_id] = value
        return resolved

    def resolve_preloaded_inputs(self, *, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        preloaded = task.get("preloaded_task_state")
        if not isinstance(preloaded, dict):
            return {}
        task_state = context.get("task_state")
        if not isinstance(task_state, dict):
            raise ValueError("context.task_state must be an object.")
        out: dict[str, Any] = {}
        for bucket_name in ("facts", "artifacts"):
            declared_bucket = preloaded.get(bucket_name)
            if not isinstance(declared_bucket, dict):
                continue
            current_bucket = task_state.get(bucket_name)
            if not isinstance(current_bucket, dict):
                raise ValueError(f"context.task_state.{bucket_name} must be an object.")
            for data_id in declared_bucket.keys():
                if not isinstance(data_id, str) or not data_id.strip():
                    raise ValueError(f"compiled_task.preloaded_task_state.{bucket_name} keys must be non-empty strings.")
                if data_id in current_bucket:
                    out[data_id] = current_bucket[data_id]
        return out

    def resolve_state_path_for_data_id(self, *, task: dict[str, Any], data_id: str) -> str:
        bindings = self.data_bindings_by_id(task=task)
        binding = bindings.get(data_id)
        if binding is not None:
            path = str(binding.get("state_path") or "").strip()
            if not path:
                raise ValueError(f"Data binding for '{data_id}' is missing state_path.")
            return path
        if data_id.startswith("fact_"):
            return f"task_state.facts.{data_id}"
        if data_id.startswith("artifact_"):
            return f"task_state.artifacts.{data_id}"
        raise ValueError(f"Unsupported data id prefix for '{data_id}'. Expected fact_* or artifact_*.")

    def data_bindings_by_id(self, *, task: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw = task.get("data_bindings")
        if raw is None:
            return {}
        if not isinstance(raw, list):
            raise ValueError("compiled_task.data_bindings must be a list when provided.")
        out: dict[str, dict[str, Any]] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("compiled_task.data_bindings entries must be objects.")
            data_id = str(item.get("data_id") or "").strip()
            if not data_id:
                raise ValueError("compiled_task.data_bindings entry missing data_id.")
            if data_id in out:
                raise ValueError(f"Duplicate data binding for data_id '{data_id}'.")
            out[data_id] = item
        return out

    def log_action_outputs(self, *, step: dict[str, Any], context: dict[str, Any]) -> None:
        step_id = str(step.get("id") or "").strip()
        declared = step.get("produces_data_ids")
        if not isinstance(declared, list) or not declared:
            logger.info("TASKIR OUTPUTS step_id=%s produced_data_ids=[]", step_id)
            return
        task_state = context.get("task_state")
        if not isinstance(task_state, dict):
            return
        facts = task_state.get("facts") if isinstance(task_state.get("facts"), dict) else {}
        artifacts = task_state.get("artifacts") if isinstance(task_state.get("artifacts"), dict) else {}
        for data_id_raw in declared:
            data_id = str(data_id_raw or "").strip()
            if not data_id:
                continue
            if data_id.startswith("fact_"):
                value = facts.get(data_id, _MISSING)
                family = "fact"
            else:
                value = artifacts.get(data_id, _MISSING)
                family = "artifact"
            if value is _MISSING:
                logger.info("TASKIR OUTPUT VALUE step_id=%s data_id=%s family=%s status=missing", step_id, data_id, family)
                continue
            logger.info(
                "TASKIR OUTPUT VALUE step_id=%s data_id=%s family=%s value=%s",
                step_id,
                data_id,
                family,
                _value_preview(value),
            )

    def resolve_task_ir_step_scope_context(
        self,
        *,
        context: dict[str, Any],
        executor: str,
    ) -> dict[str, Any]:
        inherited = context.get("_task_ir_inherited_scope_context")
        if isinstance(inherited, dict):
            payload = dict(inherited)
            resources = payload.get("resources")
            if not isinstance(resources, dict):
                resources = {}
            if not isinstance(resources.get("allowed_global_resources"), list):
                resources["allowed_global_resources"] = []
            if not isinstance(resources.get("allowed_room_resources"), list):
                resources["allowed_room_resources"] = []
            if not isinstance(resources.get("denied_resources"), list):
                resources["denied_resources"] = []
            if not isinstance(resources.get("resource_groups"), list):
                resources["resource_groups"] = []
            payload["resources"] = resources
            return payload
        fallback_scope = build_system_scope_context(
            owner_id="task_ir_runtime",
            actor_id=str(executor or "").strip() or "task_ir_executor",
            surface="task_ir",
            authority_level=95,
        )
        fallback_payload = fallback_scope.model_dump()
        fallback_payload["resources"]["allowed_global_resources"] = []
        fallback_payload["resources"]["allowed_room_resources"] = []
        fallback_payload["resources"]["denied_resources"] = []
        fallback_payload["resources"]["resource_groups"] = []
        return fallback_payload

    def resolve_task_llm_params_for_step(self, *, task: dict[str, Any], step: dict[str, Any]) -> dict[str, Any] | None:
        merged: dict[str, Any] = {}
        base = task.get("llm_params")
        if isinstance(base, dict):
            merged.update(base)
        override = step.get("llm_params")
        if isinstance(override, dict):
            merged.update(override)
        if not merged:
            return None
        TaskIRValidator.validate_task_llm_params(merged)
        out: dict[str, Any] = {}
        if "engine" in merged and merged.get("engine") is not None:
            out["engine"] = str(merged.get("engine")).strip()
        if "temperature" in merged and merged.get("temperature") is not None:
            out["temperature"] = float(merged.get("temperature"))
        if "timeout" in merged and merged.get("timeout") is not None:
            out["timeout"] = float(merged.get("timeout"))
        return out if out else None

    @staticmethod
    def get_by_dot_path(obj: dict[str, Any], path: str, *, default: Any) -> Any:
        parts = [p for p in str(path or "").split(".") if p]
        if not parts:
            return default
        cur: Any = obj
        for part in parts:
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur