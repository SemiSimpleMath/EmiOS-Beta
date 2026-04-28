from __future__ import annotations

from typing import Any


class TaskIRValidator:
    def validate_compiled_task(self, compiled_task: dict[str, Any]) -> dict[str, Any]:
        task = dict(compiled_task or {})
        if str(task.get("schema_version") or "") != "task_ir_v1":
            raise ValueError("compiled_task.schema_version must be 'task_ir_v1'")
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("compiled_task.task_id is required")
        entry_step_id = str(task.get("entry_step_id") or "").strip()
        if not entry_step_id:
            raise ValueError("compiled_task.entry_step_id is required")
        steps = task.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("compiled_task.steps must be a non-empty list")
        self.validate_task_llm_params(task.get("llm_params"))
        data_bindings = task.get("data_bindings")
        if data_bindings is not None and not isinstance(data_bindings, list):
            raise ValueError("compiled_task.data_bindings must be list when provided")
        if isinstance(data_bindings, list):
            for entry in data_bindings:
                if not isinstance(entry, dict):
                    raise ValueError("compiled_task.data_bindings entries must be objects")
                data_id = str(entry.get("data_id") or "").strip()
                state_path = str(entry.get("state_path") or "").strip()
                if not data_id:
                    raise ValueError("compiled_task.data_bindings entry missing data_id")
                if not state_path:
                    raise ValueError(f"compiled_task.data_bindings[{data_id}] missing state_path")
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("compiled_task.steps items must be objects")
            kind = str(step.get("kind") or "").strip()
            if kind not in {"action", "tool_sequence", "wait_for_event", "wait_gate", "decision", "end"}:
                raise ValueError(f"Unsupported step.kind: {kind}")
            if kind == "wait_gate":
                subscriptions = step.get("subscriptions")
                if not isinstance(subscriptions, list) or not subscriptions:
                    raise ValueError("wait_gate.subscriptions must be a non-empty list[str]")
                for item in subscriptions:
                    if not isinstance(item, str):
                        raise ValueError("wait_gate.subscriptions entries must be str")
                release_condition = str(step.get("release_condition") or "").strip()
                if not release_condition:
                    raise ValueError("wait_gate.release_condition is required")
            for field_name in ("consumes_data_ids", "produces_data_ids"):
                raw_ids = step.get(field_name)
                if raw_ids is None:
                    continue
                if not isinstance(raw_ids, list):
                    raise ValueError(f"step.{field_name} must be list[str] when provided")
                for item in raw_ids:
                    if not isinstance(item, str):
                        raise ValueError(f"step.{field_name} entries must be str")
            self.validate_task_llm_params(step.get("llm_params"), prefix="step.llm_params")
            _ = self.resolve_step_output_schema(step=step)
        task["task_id"] = task_id
        task["entry_step_id"] = entry_step_id
        task["steps"] = steps
        return task

    @staticmethod
    def validate_task_llm_params(raw: Any, *, prefix: str = "compiled_task.llm_params") -> None:
        if raw is None:
            return
        if not isinstance(raw, dict):
            raise ValueError(f"{prefix} must be an object when provided.")
        if "engine" in raw:
            engine = raw.get("engine")
            if not isinstance(engine, str) or not engine.strip():
                raise ValueError(f"{prefix}.engine must be a non-empty string when provided.")
        if "temperature" in raw and not isinstance(raw.get("temperature"), (int, float)):
            raise ValueError(f"{prefix}.temperature must be numeric when provided.")
        if "timeout" in raw and not isinstance(raw.get("timeout"), (int, float)):
            raise ValueError(f"{prefix}.timeout must be numeric when provided.")

    def resolve_step_output_schema(self, *, step: dict[str, Any]) -> dict[str, Any] | None:
        explicit = step.get("output_schema")
        if isinstance(explicit, dict):
            schema = dict(explicit)
            self.validate_output_schema_object(schema)
            return schema
        if explicit is not None:
            raise ValueError(f"Step '{step.get('id')}' output_schema must be an object when provided.")
        gate_type_raw = step.get("gate_type")
        if gate_type_raw is None:
            return None
        gate_type = str(gate_type_raw or "").strip().lower()
        if not gate_type:
            raise ValueError(f"Step '{step.get('id')}' gate_type must be non-empty when provided.")
        primitive_map = {
            "tf": "bool",
            "bool": "bool",
            "boolean": "bool",
            "int": "int",
            "integer": "int",
            "double": "double",
            "float": "double",
            "number": "double",
            "string": "string",
            "str": "string",
            "list": "list",
        }
        mapped = primitive_map.get(gate_type)
        if mapped is None:
            raise ValueError(f"Step '{step.get('id')}' has unsupported gate_type '{gate_type_raw}'.")
        schema = {"type": mapped}
        self.validate_output_schema_object(schema)
        return schema

    def validate_output_schema_object(self, schema: dict[str, Any]) -> None:
        schema_type = str(schema.get("type") or "").strip().lower()
        if schema_type not in {"bool", "int", "double", "string", "list"}:
            raise ValueError(f"output_schema.type must be one of bool/int/double/string/list, got '{schema_type}'.")
        if schema_type == "list" and "items" in schema:
            items_schema = schema.get("items")
            if isinstance(items_schema, str):
                items_schema = {"type": items_schema}
            if not isinstance(items_schema, dict):
                raise ValueError("output_schema.items must be a string type alias or object when provided.")
            self.validate_output_schema_object(items_schema)

    def validate_output_value_against_schema(
        self,
        *,
        step_id: str,
        data_id: str,
        schema: dict[str, Any],
        value: Any,
    ) -> None:
        schema_type = str(schema.get("type") or "").strip().lower()
        if schema_type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"Step '{step_id}' data '{data_id}' must be bool per output_schema.")
            return
        if schema_type == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"Step '{step_id}' data '{data_id}' must be int per output_schema.")
            return
        if schema_type == "double":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"Step '{step_id}' data '{data_id}' must be numeric per output_schema.")
            return
        if schema_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"Step '{step_id}' data '{data_id}' must be string per output_schema.")
            return
        if schema_type == "list":
            if not isinstance(value, list):
                raise ValueError(f"Step '{step_id}' data '{data_id}' must be list per output_schema.")
            items_schema = schema.get("items")
            if items_schema is None:
                return
            if isinstance(items_schema, str):
                normalized_items_schema = {"type": items_schema}
            elif isinstance(items_schema, dict):
                normalized_items_schema = dict(items_schema)
            else:
                raise ValueError("output_schema.items must be string alias or object when provided.")
            self.validate_output_schema_object(normalized_items_schema)
            for idx, item in enumerate(value):
                self.validate_output_value_against_schema(
                    step_id=step_id,
                    data_id=f"{data_id}[{idx}]",
                    schema=normalized_items_schema,
                    value=item,
                )
            return
        raise ValueError(f"Unsupported output_schema.type '{schema_type}'.")