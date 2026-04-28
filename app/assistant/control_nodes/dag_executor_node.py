from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import get_pending_tool, get_scratch, set_pending_tool, set_scratch
from app.assistant.utils.pydantic_classes import Message, ToolMessage, ToolResult

logger = get_logger(__name__)


class DagExecutorNode(ControlNode):
    STATE_KEY = "multi_tool_agent_dag_runtime"

    def action_handler(self, message):
        state = self._load_state()
        if not isinstance(state, dict):
            self._publish_result({"ok": False, "error": "Missing DAG execution state."})
            return

        if bool(state.get("waiting_for_result")):
            tool_result = self._latest_tool_result()
            if not isinstance(tool_result, dict):
                self._publish_result({"ok": False, "error": "Expected tool_result, found none."}, state)
                return
            step_id = str(state.get("current_step_id") or "")
            state.setdefault("results", {})[step_id] = tool_result
            state.setdefault("order", []).append(step_id)
            if not bool(tool_result.get("ok", True)):
                state.setdefault("status_by_step", {})[step_id] = "failed"
                self._save_state(state)
                self._publish_result(
                    {
                        "ok": False,
                        "error": tool_result.get("error") or "Tool step failed.",
                        "steps": state.get("results", {}),
                        "order": state.get("order", []),
                    },
                    state,
                )
                return
            state.setdefault("status_by_step", {})[step_id] = "done"
            state["waiting_for_result"] = False
            state["current_step_id"] = None
            self._save_state(state)

        sequence = state.get("sequence") if isinstance(state.get("sequence"), list) else []
        if self._all_done(state):
            terminal_outputs = self._terminal_outputs(sequence, state)
            self._publish_result(
                {
                    "ok": True,
                    "task_done": True,
                    "terminal_outputs": terminal_outputs,
                    "terminal_outputs_text": self._terminal_outputs_text(terminal_outputs),
                    "steps": state.get("results", {}),
                    "order": state.get("order", []),
                },
                state,
            )
            return

        next_pair = self._next_ready_step(sequence, state)
        if next_pair is None:
            pending = [k for k, v in (state.get("status_by_step") or {}).items() if v == "pending"]
            self._publish_result(
                {
                    "ok": False,
                    "error": f"No runnable steps found. Pending={pending}.",
                    "steps": state.get("results", {}),
                    "order": state.get("order", []),
                },
                state,
            )
            return

        step, step_id = next_pair
        tool_name = str(step.get("tool_name") or "").strip()
        if tool_name in {"execute_dag", "return_control"}:
            self._publish_result(
                {
                    "ok": False,
                    "error": f"Invalid tool name in DAG step '{step_id}': {tool_name}",
                    "step": step,
                    "steps": state.get("results", {}),
                    "order": state.get("order", []),
                },
                state,
            )
            return
        if not tool_name:
            self._publish_result({"ok": False, "error": f"Step '{step_id}' missing tool_name."}, state)
            return
        raw_arguments = self._materialize_step_arguments(step)
        has_arguments_json = isinstance(step.get("arguments_json"), str) and bool(step.get("arguments_json").strip())
        arguments = self._resolve_step_arguments(raw_arguments, state, step)

        if isinstance(arguments, dict):
            set_pending_tool(
                self.blackboard,
                name=tool_name,
                calling_agent=self.name,
                arguments=arguments,
                action_input=None,
                kind="tool",
            )
            self.blackboard.update_state_value("next_agent", "tool_caller")
        else:
            if has_arguments_json:
                self._publish_result(
                    {
                        "ok": False,
                        "error": f"Malformed arguments_json for step '{step_id}'.",
                        "step": step,
                        "steps": state.get("results", {}),
                        "order": state.get("order", []),
                    },
                    state,
                )
                return
            set_pending_tool(
                self.blackboard,
                name=tool_name,
                calling_agent=self.name,
                arguments=None,
                action_input=self._step_action_input(step, state),
                kind="tool",
            )
            self.blackboard.update_state_value("next_agent", "shared::tool_arguments")

        state["waiting_for_result"] = True
        state["current_step_id"] = step_id
        state.setdefault("status_by_step", {})[step_id] = "queued"
        self._save_state(state)
        self.blackboard.update_state_value("last_agent", self.name)

    def _publish_result(self, result: Dict[str, Any], state: Dict[str, Any] | None = None) -> None:
        planner_name = None
        if isinstance(state, dict):
            planner_name = state.get("planner_name")
        self.blackboard.update_state_value("result", result)

        # Clear active DAG execution state once this DAG run finishes.
        self._clear_state()

        content = json.dumps({"tool_result": result}, ensure_ascii=False)
        msg_data = {
            "result_type": "dag_result",
            "data": result,
        }
        self.blackboard.add_msg(
            Message(
                data_type="tool_result",
                sub_data_type=["dag_result"],
                sender=self.name,
                receiver=planner_name if isinstance(planner_name, str) else None,
                content=content,
                data=msg_data,
            )
        )

        if isinstance(planner_name, str) and planner_name.strip():
            set_scratch(self.blackboard, f"{planner_name}::dag_result", result)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("next_agent", planner_name)
        else:
            # Fallback: no planner to return to; preserve previous exit behavior.
            self.blackboard.update_state_value("last_agent", f"{self.name}_return_control")
            self.blackboard.update_state_value("next_agent", None)
        logger.info("[%s] final DAG result: %s", self.name, json.dumps(result, ensure_ascii=False))

    def _load_state(self) -> Dict[str, Any] | None:
        state = get_scratch(self.blackboard, self.STATE_KEY, None)
        return state if isinstance(state, dict) else None

    def _save_state(self, state: Dict[str, Any]) -> None:
        set_scratch(self.blackboard, self.STATE_KEY, state)

    def _clear_state(self) -> None:
        set_scratch(self.blackboard, self.STATE_KEY, None)

    def _latest_tool_result(self) -> Dict[str, Any] | None:
        try:
            scope_id = self.blackboard.get_current_scope_id()
            msgs = self.blackboard.get_messages_for_scope(scope_id) if scope_id else []
        except Exception as e:
            logger.debug("[%s] Failed to read blackboard messages for tool result: %s", self.name, e)
            msgs = []
        if not isinstance(msgs, list):
            return None
        for msg in reversed(msgs):
            if getattr(msg, "data_type", None) != "tool_result":
                continue
            data = getattr(msg, "data", None)
            content = getattr(msg, "content", "") or ""
            metadata = getattr(msg, "metadata", None)
            if isinstance(metadata, dict):
                path = metadata.get("path")
                if isinstance(path, str) and path.strip():
                    artifact = self._read_tool_result_artifact(path.strip())
                    if isinstance(artifact, dict):
                        return artifact
            if isinstance(data, dict):
                result_type = str(data.get("result_type") or "")
                is_error = result_type == "error"
                return {
                    "ok": not is_error,
                    "result_type": result_type or None,
                    "data": data,
                    "content": content,
                    "error": content if is_error else None,
                }
            is_error = False
            return {"ok": not is_error, "content": str(content), "error": str(content) if is_error else None}
        return None

    def _read_tool_result_artifact(self, path: str) -> Dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            tr = payload.get("tool_result") if isinstance(payload, dict) else None
            if not isinstance(tr, dict):
                return None
            result_type = str(tr.get("result_type") or "")
            content = str(tr.get("content") or "")
            data = tr.get("data")
            is_error = result_type == "error"
            return {
                "ok": not is_error,
                "result_type": result_type or None,
                "content": content,
                "data": data if isinstance(data, dict) else tr.get("data"),
                "error": content if is_error else None,
            }
        except Exception:
            return None

    def _materialize_step_arguments(self, step: Dict[str, Any]) -> Dict[str, Any] | None:
        if isinstance(step.get("arguments"), dict):
            return step.get("arguments")
        if isinstance(step.get("arguments"), str) and step.get("arguments").strip():
            parsed = self._parse_arguments_json(step.get("arguments"))
            if isinstance(parsed, dict):
                return parsed
        raw = step.get("arguments_json")
        if isinstance(raw, str) and raw.strip():
            parsed = self._parse_arguments_json(raw)
            if isinstance(parsed, dict):
                return parsed
        return None

    def _parse_arguments_json(self, raw: str) -> Dict[str, Any] | None:
        text = raw.strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        # Fallback: re-escape literal control characters that may appear after
        # decoding nested JSON strings (common for multi-line content fields).
        try:
            escaped_ctrl = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
            obj = json.loads(escaped_ctrl)
            if isinstance(obj, dict):
                logger.warning("[%s] Recovered malformed arguments_json via control-char escaping.", self.name)
                return obj
        except Exception:
            pass

        # Fallback: escape invalid backslashes (common in Windows paths).
        try:
            sanitized = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text)
            obj = json.loads(sanitized)
            if isinstance(obj, dict):
                logger.warning("[%s] Recovered malformed arguments_json via backslash sanitization.", self.name)
                return obj
        except Exception:
            pass

        # Combined fallback for nested JSON strings with both issues.
        try:
            escaped_ctrl = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
            combined = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", escaped_ctrl)
            obj = json.loads(combined)
            if isinstance(obj, dict):
                logger.warning(
                    "[%s] Recovered malformed arguments_json via combined control-char + backslash sanitization.",
                    self.name,
                )
                return obj
        except Exception:
            pass

        # Last-resort extraction for common write_text_file payloads when JSON is
        # almost valid but contains problematic escapes in long prose content.
        extracted = self._extract_jsonish_write_args(text)
        if isinstance(extracted, dict):
            logger.warning("[%s] Recovered malformed arguments_json via write-args extraction.", self.name)
            return extracted
        return None

    def _extract_jsonish_write_args(self, text: str) -> Dict[str, Any] | None:
        try:
            fp = re.search(r'"file_path"\s*:\s*"((?:\\.|[^"\\])*)"', text, flags=re.DOTALL)
            ct = re.search(
                r'"content"\s*:\s*"((?:\\.|[^"\\])*)"\s*,\s*"ensure_newline"',
                text,
                flags=re.DOTALL,
            )
            en = re.search(r'"ensure_newline"\s*:\s*(true|false|null)', text, flags=re.IGNORECASE)
            if not fp or not ct:
                return None

            def _decode_jsonish(s: str) -> str:
                try:
                    return json.loads(f'"{s}"')
                except Exception:
                    s2 = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", s)
                    return json.loads(f'"{s2}"')

            file_path = _decode_jsonish(fp.group(1))
            content = _decode_jsonish(ct.group(1))
            ensure = True
            if en:
                t = str(en.group(1)).lower()
                if t == "false":
                    ensure = False
                elif t == "null":
                    ensure = True
            return {"file_path": file_path, "content": content, "ensure_newline": ensure}
        except Exception:
            return None

    def _resolve_step_arguments(
        self,
        arguments: Dict[str, Any] | None,
        state: Dict[str, Any],
        step: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        if not isinstance(arguments, dict):
            return arguments
        results = state.get("results", {}) if isinstance(state.get("results"), dict) else {}
        seq_inputs = state.get("sequence_inputs") if isinstance(state.get("sequence_inputs"), dict) else {}
        seq_id = str(step.get("_sequence_id") or "").strip() if isinstance(step, dict) else ""
        return self._resolve_value(arguments, results, seq_inputs, seq_id)

    def _resolve_value(
        self,
        value: Any,
        results: Dict[str, Any],
        sequence_inputs: Dict[str, Dict[str, str]],
        sequence_id: str,
    ) -> Any:
        if isinstance(value, str):
            return self._resolve_string(value, results, sequence_inputs, sequence_id)
        if isinstance(value, list):
            return [self._resolve_value(v, results, sequence_inputs, sequence_id) for v in value]
        if isinstance(value, dict):
            return {k: self._resolve_value(v, results, sequence_inputs, sequence_id) for k, v in value.items()}
        return value

    def _resolve_string(
        self,
        text: str,
        results: Dict[str, Any],
        sequence_inputs: Dict[str, Dict[str, str]],
        sequence_id: str,
    ) -> Any:
        pattern_full = re.compile(r"^\{\{\s*steps\.([a-zA-Z0-9_\-:]+)\.([a-zA-Z0-9_\.\-]+)\s*\}\}$")
        mfull = pattern_full.match(text.strip())
        if mfull:
            return self._lookup_step_value(results, mfull.group(1), mfull.group(2))

        seq_full = re.compile(r"^\{\{\s*sequence\.inputs\.([a-zA-Z0-9_\.\-]+)\s*\}\}$")
        sfull = seq_full.match(text.strip())
        if sfull and sequence_id:
            return self._lookup_sequence_input(sequence_inputs, sequence_id, sfull.group(1))

        pattern = re.compile(r"\{\{\s*steps\.([a-zA-Z0-9_\-:]+)\.([a-zA-Z0-9_\.\-]+)\s*\}\}")
        seq_pattern = re.compile(r"\{\{\s*sequence\.inputs\.([a-zA-Z0-9_\.\-]+)\s*\}\}")

        def repl(match):
            val = self._lookup_step_value(results, match.group(1), match.group(2))
            return "" if val is None else str(val)

        def seq_repl(match):
            val = self._lookup_sequence_input(sequence_inputs, sequence_id, match.group(1))
            return "" if val is None else str(val)

        return seq_pattern.sub(seq_repl, pattern.sub(repl, text))

    def _lookup_step_value(self, results: Dict[str, Any], step_id: str, dotted_path: str) -> Any:
        node = results.get(step_id) if isinstance(results, dict) else None
        if not isinstance(node, dict):
            return None
        cur: Any = node
        for part in dotted_path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    def _lookup_sequence_input(self, sequence_inputs: Dict[str, Dict[str, str]], sequence_id: str, dotted_path: str) -> Any:
        seq = sequence_inputs.get(sequence_id) if isinstance(sequence_inputs, dict) else None
        if not isinstance(seq, dict):
            return None
        cur: Any = seq
        for part in dotted_path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
        return cur

    def _all_done(self, state: Dict[str, Any]) -> bool:
        status = state.get("status_by_step") if isinstance(state.get("status_by_step"), dict) else {}
        return bool(status) and all(v == "done" for v in status.values())

    def _next_ready_step(self, sequence: List[Dict[str, Any]], state: Dict[str, Any]) -> tuple[Dict[str, Any], str] | None:
        status = state.get("status_by_step") if isinstance(state.get("status_by_step"), dict) else {}
        for i, raw_step in enumerate(sequence, start=1):
            step = raw_step if isinstance(raw_step, dict) else {}
            step_id = str(step.get("id") or f"step_{i}")
            if status.get(step_id) != "pending":
                continue
            deps = step.get("depends_on") if isinstance(step.get("depends_on"), list) else []
            dep_ids = [str(d).strip() for d in deps if isinstance(d, str) and str(d).strip()]
            if all(status.get(did) == "done" for did in dep_ids):
                return step, step_id
        return None

    def _step_action_input(self, step: Dict[str, Any], state: Dict[str, Any]) -> Any:
        explicit = step.get("action_input")
        if explicit is not None:
            return explicit
        deps = step.get("depends_on") if isinstance(step.get("depends_on"), list) else []
        dep_id = str(deps[0]).strip() if deps and isinstance(deps[0], str) else ""
        prev = None
        if dep_id:
            prev = (state.get("results") or {}).get(dep_id)
        seq_id = str(step.get("_sequence_id") or "").strip()
        initial = ""
        if seq_id:
            seq_inputs = state.get("sequence_inputs") if isinstance(state.get("sequence_inputs"), dict) else {}
            seq_map = seq_inputs.get(seq_id) if isinstance(seq_inputs, dict) else {}
            if isinstance(seq_map, dict):
                initial = str(seq_map.get("initial_input") or "")
        payload = {
            "instruction": "Use previous tool result as primary input for this tool.",
            "sequence_id": seq_id,
            "previous_step_id": dep_id or None,
            "previous_result": prev,
            "initial_input": initial,
            "current_tool": str(step.get("tool_name") or ""),
        }
        return json.dumps(payload, ensure_ascii=False)

    def _terminal_outputs(self, sequence: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
        depended = set()
        for raw_step in sequence:
            st = raw_step if isinstance(raw_step, dict) else {}
            deps = st.get("depends_on") if isinstance(st.get("depends_on"), list) else []
            for d in deps:
                if isinstance(d, str) and d.strip():
                    depended.add(d.strip())
        terminals: Dict[str, Any] = {}
        results = state.get("results") if isinstance(state.get("results"), dict) else {}
        for raw_step in sequence:
            st = raw_step if isinstance(raw_step, dict) else {}
            sid = str(st.get("id") or "").strip()
            if not sid or sid in depended:
                continue
            terminals[sid] = results.get(sid)
        return terminals

    def _terminal_outputs_text(self, terminal_outputs: Dict[str, Any]) -> str:
        lines: List[str] = []
        for sid, res in (terminal_outputs or {}).items():
            if not isinstance(res, dict):
                continue
            content = str(res.get("content") or "").strip()
            lines.append(f"[{sid}] {content}")
        return "\n".join(lines).strip()

    # ----------------------------
    # Sequence-scope sync executor
    # ----------------------------
    def execute_sequence_sync(
        self,
        *,
        sequence_id: str,
        steps: List[Dict[str, Any]],
        initial_input: str,
        task_text: str,
    ) -> Dict[str, Any]:
        local_state: Dict[str, Any] = {
            "results": {},
            "sequence_inputs": {sequence_id: {"initial_input": str(initial_input or "")}},
        }
        order: List[str] = []
        last_step_id: str | None = None
        for i, raw in enumerate(steps, start=1):
            step = raw if isinstance(raw, dict) else {}
            step_id = str(step.get("id") or f"{sequence_id}::step{i}")
            last_step_id = step_id
            callable_name = str(step.get("tool_name") or "").strip()
            if not callable_name:
                return {
                    "ok": False,
                    "error": f"Step '{step_id}' missing tool_name.",
                    "sequence_id": sequence_id,
                    "steps": local_state.get("results", {}),
                    "order": order,
                    "terminal_step_id": last_step_id,
                }
            callable_kind = self._resolve_callable_kind(callable_name)
            if callable_kind is None:
                return {
                    "ok": False,
                    "error": f"Step '{step_id}' references unknown callable '{callable_name}'.",
                    "sequence_id": sequence_id,
                    "steps": local_state.get("results", {}),
                    "order": order,
                    "terminal_step_id": last_step_id,
                }

            action_input = step.get("action_input")
            deps = step.get("depends_on") if isinstance(step.get("depends_on"), list) else []
            is_entry_step = not any(isinstance(d, str) and d.strip() for d in deps)
            if callable_kind == "tool":
                raw_arguments = self._materialize_step_arguments(step)
                arguments = self._resolve_step_arguments(raw_arguments, local_state, step)
                if is_entry_step:
                    # Deterministic entrypoint contract:
                    # sequence[i] uses action_input[i] via ToolArguments, no heuristic fallback.
                    ta_args = self._resolve_with_tool_arguments_agent(
                        tool_name=callable_name,
                        action_input=action_input,
                        task_text=task_text,
                    )
                    arguments = self._normalize_tool_arguments_payload(ta_args)
                    arguments = self._schema_resolve_arguments(
                        tool_name=callable_name,
                        arguments=arguments if isinstance(arguments, dict) else None,
                        action_input=None,
                        local_state=local_state,
                        sequence_id=sequence_id,
                        task_text=task_text,
                        fill_missing_required=False,
                        include_action_input=False,
                    )
                else:
                    arguments = self._schema_resolve_arguments(
                        tool_name=callable_name,
                        arguments=arguments if isinstance(arguments, dict) else None,
                        action_input=action_input,
                        local_state=local_state,
                        sequence_id=sequence_id,
                        task_text=task_text,
                    )
                    if not isinstance(arguments, dict):
                        ta_args = self._resolve_with_tool_arguments_agent(
                            tool_name=callable_name,
                            action_input=action_input,
                            task_text=task_text,
                        )
                        arguments = self._normalize_tool_arguments_payload(ta_args)
                    if not isinstance(arguments, dict):
                        arguments = self._infer_arguments(
                            step=step,
                            local_state=local_state,
                            sequence_id=sequence_id,
                            task_text=task_text,
                        )
            else:
                arguments = self._build_non_tool_arguments(
                    step=step,
                    local_state=local_state,
                    sequence_id=sequence_id,
                    step_id=step_id,
                    action_input=action_input,
                )
            if not isinstance(arguments, dict):
                return {
                    "ok": False,
                    "error": f"No executable arguments for step '{step_id}' ({callable_kind}:{callable_name}).",
                    "sequence_id": sequence_id,
                    "steps": local_state.get("results", {}),
                    "order": order,
                    "terminal_step_id": last_step_id,
                }

            step_result = self._execute_callable_direct(
                callable_name=callable_name,
                callable_kind=callable_kind,
                arguments=arguments,
            )
            local_state["results"][step_id] = step_result
            order.append(step_id)
            if not bool(step_result.get("ok", True)):
                return {
                    "ok": False,
                    "error": step_result.get("error") or f"Step failed: {step_id}",
                    "sequence_id": sequence_id,
                    "steps": local_state.get("results", {}),
                    "order": order,
                    "terminal_step_id": last_step_id,
                }

        terminal = local_state["results"].get(last_step_id) if isinstance(last_step_id, str) else None
        return {
            "ok": True,
            "task_done": True,
            "sequence_id": sequence_id,
            "terminal_step_id": last_step_id,
            "terminal_output": terminal if isinstance(terminal, dict) else None,
            "steps": local_state.get("results", {}),
            "order": order,
        }

    def _resolve_callable_kind(self, callable_name: str) -> str | None:
        tool_cls = self.tool_registry.get_tool_class(callable_name)
        if tool_cls is not None:
            return "tool"

        cfg = self.agent_registry.get_agent_config(callable_name)
        if not isinstance(cfg, dict):
            return None
        cfg_type = str(cfg.get("type") or "agent").strip().lower()
        if cfg_type == "control_node":
            return "control_node"
        return "agent"

    def _build_non_tool_arguments(
        self,
        *,
        step: Dict[str, Any],
        local_state: Dict[str, Any],
        sequence_id: str,
        step_id: str,
        action_input: Any,
    ) -> Dict[str, Any] | None:
        raw_arguments = self._materialize_step_arguments(step)
        arguments = self._resolve_step_arguments(raw_arguments, local_state, step)
        if not isinstance(arguments, dict):
            parsed_action_input = self._parse_action_input_dict(action_input)
            if isinstance(parsed_action_input, dict):
                arguments = parsed_action_input
        if not isinstance(arguments, dict):
            if isinstance(action_input, str) and action_input.strip():
                arguments = {"action_input": action_input.strip()}
            else:
                arguments = {}

        deps = step.get("depends_on") if isinstance(step.get("depends_on"), list) else []
        dep_id = str(deps[0]).strip() if deps and isinstance(deps[0], str) else ""
        if dep_id:
            prev = local_state.get("results", {}).get(dep_id)
            if not isinstance(prev, dict):
                return None
            arguments.setdefault("previous_step_id", dep_id)
            arguments.setdefault("previous_result", prev)

        arguments.setdefault("sequence_id", sequence_id)
        arguments.setdefault("step_id", step_id)
        return arguments

    def _execute_callable_direct(
        self,
        *,
        callable_name: str,
        callable_kind: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        if callable_kind == "tool":
            return self._execute_tool_direct(tool_name=callable_name, arguments=arguments)
        return self._execute_agent_like_direct(callable_name=callable_name, arguments=arguments)

    def _execute_tool_direct(self, *, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        try:
            tool_cls = self.tool_registry.get_tool_class(tool_name)
            if tool_cls is None:
                return {"ok": False, "result_type": "error", "content": "", "error": f"tool not found: {tool_name}"}
            instance = tool_cls()
            msg = ToolMessage(tool_name=tool_name, tool_data={"tool_name": tool_name, "arguments": deepcopy(arguments)})
            res = instance.execute(msg)
            if not isinstance(res, ToolResult):
                return {"ok": False, "result_type": "error", "content": "", "error": f"invalid ToolResult from {tool_name}"}
            is_error = str(res.result_type or "") == "error"
            return {
                "ok": not is_error,
                "result_type": res.result_type,
                "content": res.content,
                "data": res.data if isinstance(res.data, dict) else res.data,
                "error": (res.content or "") if is_error else None,
            }
        except Exception as e:
            logger.error("[%s] Tool callable '%s' failed during DAG sequence execution", self.name, tool_name)
            logger.debug("[%s] tool callable '%s' DAG execution exception details", self.name, tool_name, exc_info=True)
            return {"ok": False, "result_type": "error", "content": str(e), "error": str(e)}

    def _execute_agent_like_direct(self, *, callable_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        branch_bb = Blackboard()
        branch_bb.update_state_value("task", self.blackboard.get_state_value("task"))
        branch_bb.update_state_value("information", self.blackboard.get_state_value("information"))
        branch_bb.update_state_value("date_time", self.blackboard.get_state_value("date_time"))

        instance = self._instantiate_callable_agent(callable_name=callable_name, blackboard=branch_bb)
        if instance is None:
            return {
                "ok": False,
                "result_type": "error",
                "content": "",
                "error": f"callable not instantiable: {callable_name}",
            }
        try:
            msg = Message(agent_input=deepcopy(arguments), data=deepcopy(arguments))
            res = instance.action_handler(msg)
            return self._normalize_agent_like_result(callable_name=callable_name, result=res, blackboard=branch_bb)
        except Exception as e:
            logger.error("[%s] Callable '%s' failed during DAG sequence execution", self.name, callable_name)
            logger.debug("[%s] callable '%s' DAG execution exception details", self.name, callable_name, exc_info=True)
            return {"ok": False, "result_type": "error", "content": str(e), "error": str(e)}

    def _instantiate_callable_agent(self, *, callable_name: str, blackboard: Blackboard):
        try:
            agent_class = self.agent_registry.get_agent_class(callable_name)
            if agent_class is None:
                return None
            cfg = self.agent_registry.get_agent_config(callable_name)
            llm_params = {}
            if isinstance(cfg, dict) and isinstance(cfg.get("llm_params"), dict):
                llm_params = dict(cfg.get("llm_params") or {})
            return agent_class(
                name=callable_name,
                blackboard=blackboard,
                agent_registry=self.agent_registry,
                tool_registry=self.tool_registry,
                llm_params=llm_params,
            )
        except Exception as e:
            logger.error(
                "[%s] Failed to instantiate callable '%s' for DAG sequence execution",
                self.name,
                callable_name,
            )
            logger.debug("[%s] callable instantiation exception details", self.name, exc_info=True)
            return None

    def _normalize_agent_like_result(
        self,
        *,
        callable_name: str,
        result: Any,
        blackboard: Blackboard,
    ) -> Dict[str, Any]:
        if isinstance(result, ToolResult):
            is_error = str(result.result_type or "") == "error"
            return {
                "ok": not is_error,
                "result_type": result.result_type or "tool_result",
                "content": str(result.content or ""),
                "data": result.data if isinstance(result.data, dict) else result.data,
                "error": str(result.content or "") if is_error else None,
            }

        if isinstance(result, dict):
            is_error = bool(result.get("ok") is False) or str(result.get("result_type") or "") == "error"
            content = str(result.get("content") or result.get("error") or "").strip()
            return {
                "ok": not is_error,
                "result_type": str(result.get("result_type") or ("error" if is_error else "agent_result")),
                "content": content,
                "data": result,
                "error": content if is_error else None,
            }

        bb_result = blackboard.get_state_value("result")
        if isinstance(bb_result, dict):
            is_error = bool(bb_result.get("ok") is False) or str(bb_result.get("result_type") or "") == "error"
            content = str(bb_result.get("content") or bb_result.get("error") or "").strip()
            return {
                "ok": not is_error,
                "result_type": str(bb_result.get("result_type") or ("error" if is_error else "agent_result")),
                "content": content,
                "data": bb_result,
                "error": content if is_error else None,
            }

        content = str(result or "").strip()
        return {
            "ok": True,
            "result_type": "agent_result",
            "content": content,
            "data": {"value": content, "callable": callable_name},
            "error": None,
        }

    def _infer_arguments(
        self,
        *,
        step: Dict[str, Any],
        local_state: Dict[str, Any],
        sequence_id: str,
        task_text: str,
    ) -> Dict[str, Any] | None:
        tool_name = str(step.get("tool_name") or "").strip()
        action_input = step.get("action_input")
        if not isinstance(action_input, str) or not action_input.strip():
            seq_inputs = local_state.get("sequence_inputs") if isinstance(local_state.get("sequence_inputs"), dict) else {}
            seq_map = seq_inputs.get(sequence_id) if isinstance(seq_inputs, dict) else {}
            if isinstance(seq_map, dict):
                action_input = str(seq_map.get("initial_input") or "")
            else:
                action_input = ""

        if tool_name == "set_screen_capture_enabled":
            return {"enabled": True}

        if tool_name == "capture_monitor_screenshot":
            monitor = self._extract_monitor_index(str(action_input), fallback_text=str(task_text))
            if monitor is None:
                # Stable fallback for sequence-style monitor captures:
                # seq1 -> monitor 2, seq2 -> monitor 3, ...
                seq_num = self._sequence_index(sequence_id)
                monitor = (seq_num + 1) if seq_num is not None else 2
            return {"monitor_index": int(monitor)}

        if tool_name == "vision_image_describe":
            image_path = self._latest_image_path(local_state)
            if not image_path:
                return None
            return {"image_path": image_path, "question": "Describe this image concisely."}

        if tool_name == "write_text_file":
            content = str(action_input or "").strip()
            if not content:
                content = self._results_rollup(local_state)
            file_path = self._extract_target_file_path(str(task_text))
            if not file_path:
                file_path = "resources/dayflow_pipeline_outputs/resource_desktop_activity_recent.md"
            return {"file_path": file_path, "content": content, "ensure_newline": True}

        if tool_name == "find_tool":
            query = str(action_input or "").strip()
            return {"query": query, "tool_kind": "all", "limit": 5}

        if tool_name == "install_tool":
            selected = str(action_input or "").strip()
            if selected:
                return {"tool_name": selected, "install_source": "dag_executor"}
            return None

        return None

    def _schema_resolve_arguments(
        self,
        *,
        tool_name: str,
        arguments: Dict[str, Any] | None,
        action_input: Any,
        local_state: Dict[str, Any],
        sequence_id: str,
        task_text: str,
        fill_missing_required: bool = True,
        include_action_input: bool = True,
    ) -> Dict[str, Any] | None:
        req = self._required_argument_fields(tool_name)
        if req is None:
            return arguments
        candidate: Dict[str, Any] = dict(arguments or {})
        if include_action_input:
            ai = self._parse_action_input_dict(action_input)
            if isinstance(ai, dict):
                if isinstance(ai.get("arguments"), dict):
                    for k, v in ai["arguments"].items():
                        candidate.setdefault(str(k), v)
                for k, v in ai.items():
                    if k == "arguments":
                        continue
                    candidate.setdefault(str(k), v)

        self._apply_argument_aliases(candidate)
        if fill_missing_required:
            self._fill_missing_required(
                candidate=candidate,
                required=req,
                tool_name=tool_name,
                local_state=local_state,
                sequence_id=sequence_id,
                task_text=task_text,
            )
        missing = [k for k in req if k not in candidate or candidate.get(k) is None]
        if missing:
            return None
        return candidate

    def _normalize_tool_arguments_payload(self, payload: Any) -> Dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        if isinstance(payload.get("arguments"), dict):
            return dict(payload.get("arguments") or {})
        return dict(payload)

    def _required_argument_fields(self, tool_name: str) -> List[str] | None:
        try:
            form = self.tool_registry.get_tool_form(tool_name)
        except Exception as e:
            logger.debug("[%s] Could not get tool form for %r: %s", self.name, tool_name, e)
            form = None
        if form is None or not hasattr(form, "model_fields"):
            return None
        args_field = form.model_fields.get("arguments")  # outer wrapper: {tool_name, arguments}
        if args_field is None:
            return None
        inner_model = getattr(args_field, "annotation", None)
        if inner_model is None or not hasattr(inner_model, "model_fields"):
            return None
        required: List[str] = []
        for fname, finfo in inner_model.model_fields.items():
            if bool(getattr(finfo, "is_required", lambda: False)()):
                required.append(str(fname))
        return required

    def _parse_action_input_dict(self, action_input: Any) -> Dict[str, Any] | None:
        if isinstance(action_input, dict):
            return action_input
        if isinstance(action_input, str) and action_input.strip():
            text = action_input.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    obj = json.loads(text)
                    if isinstance(obj, dict):
                        return obj
                except Exception:
                    return None
        return None

    def _apply_argument_aliases(self, candidate: Dict[str, Any]) -> None:
        aliases = {
            "monitor": "monitor_index",
            "image": "image_path",
            "path": "file_path",
            "text": "content",
        }
        for src, dst in aliases.items():
            if src in candidate and dst not in candidate:
                candidate[dst] = candidate[src]

    def _fill_missing_required(
        self,
        *,
        candidate: Dict[str, Any],
        required: List[str],
        tool_name: str,
        local_state: Dict[str, Any],
        sequence_id: str,
        task_text: str,
    ) -> None:
        if "monitor_index" in required and "monitor_index" not in candidate:
            seq_num = self._sequence_index(sequence_id)
            candidate["monitor_index"] = (seq_num + 1) if seq_num is not None else 2
        if "image_path" in required and "image_path" not in candidate:
            ip = self._latest_image_path(local_state)
            if isinstance(ip, str) and ip.strip():
                candidate["image_path"] = ip.strip()
        if "file_path" in required and "file_path" not in candidate:
            fp = self._extract_target_file_path(task_text)
            if isinstance(fp, str) and fp.strip():
                candidate["file_path"] = fp.strip()
        if "content" in required and "content" not in candidate:
            content = self._results_rollup(local_state)
            if content:
                candidate["content"] = content
        if tool_name == "set_screen_capture_enabled" and "enabled" in required and "enabled" not in candidate:
            candidate["enabled"] = True

    def _resolve_with_tool_arguments_agent(
        self,
        *,
        tool_name: str,
        action_input: Any,
        task_text: str,
    ) -> Dict[str, Any] | None:
        """
        Run an isolated ToolArguments pass for this executor branch.
        This avoids cross-branch contention on the manager's global pipeline_state.
        """
        try:
            branch_bb = Blackboard()
            branch_bb.update_state_value("task", task_text)
            branch_bb.update_state_value("information", self.blackboard.get_state_value("information"))
            branch_bb.update_state_value("date_time", self.blackboard.get_state_value("date_time"))

            set_pending_tool(
                branch_bb,
                name=tool_name,
                calling_agent="dag_executor_node",
                arguments=None,
                action_input=action_input,
                kind="tool",
            )

            agent = self._instantiate_callable_agent(callable_name="shared::tool_arguments", blackboard=branch_bb)
            if agent is None:
                return None
            agent.action_handler(Message(agent_input={}))
            pending = get_pending_tool(branch_bb) or {}
            args = pending.get("arguments")
            return args if isinstance(args, dict) else None
        except Exception as e:
            logger.warning("[%s] ToolArguments resolver failed for '%s': %s", self.name, tool_name, e)
            logger.debug("[%s] ToolArguments resolver exception details", self.name, exc_info=True)
            return None

    def _extract_monitor_index(self, text: str, fallback_text: str = "") -> int | None:
        for src in (text, fallback_text):
            if not isinstance(src, str):
                continue
            # Accept compact JSON inputs like {"monitor_index": 3}.
            s = src.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    obj = json.loads(s)
                    mi = obj.get("monitor_index") if isinstance(obj, dict) else None
                    if isinstance(mi, int):
                        return int(mi)
                    if isinstance(mi, str) and mi.strip().isdigit():
                        return int(mi.strip())
                except Exception:
                    pass
            m = re.search(r"monitor\s*(\d+)", src, flags=re.IGNORECASE)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    continue
        return None

    def _sequence_index(self, sequence_id: str) -> int | None:
        m = re.match(r"^seq(\d+)$", str(sequence_id or "").strip(), flags=re.IGNORECASE)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    def _latest_image_path(self, local_state: Dict[str, Any]) -> str | None:
        results = local_state.get("results") if isinstance(local_state.get("results"), dict) else {}
        for sid in reversed(list(results.keys())):
            node = results.get(sid) if isinstance(results.get(sid), dict) else {}
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            image_path = data.get("image_path")
            if isinstance(image_path, str) and image_path.strip():
                return image_path.strip()
        return None

    def _extract_target_file_path(self, task_text: str) -> str | None:
        if not isinstance(task_text, str) or not task_text.strip():
            return None
        m = re.search(r"(resources/[a-zA-Z0-9_\-/.]+\.md)", task_text)
        if m:
            return m.group(1)
        return None

    def _results_rollup(self, local_state: Dict[str, Any]) -> str:
        out: List[str] = []
        results = local_state.get("results") if isinstance(local_state.get("results"), dict) else {}
        for sid, node in results.items():
            if not isinstance(node, dict):
                continue
            content = str(node.get("content") or "").strip()
            if content:
                out.append(f"[{sid}] {content}")
        return "\n".join(out).strip()

