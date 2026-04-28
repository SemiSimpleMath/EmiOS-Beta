from __future__ import annotations

from typing import Any, Dict, List

from app.assistant.agent_classes.Planner import Planner
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.pipeline_state import set_scratch

logger = get_logger(__name__)


class MultiToolAgent(Planner):
    """
    Experimental agent for running multi-step tool sequences with minimal LLM hops.

    This class is intentionally small and safe:
    - no special routing logic
    - no manager integration changes yet
    - can be called directly from tests/experiments
    """

    DAG_RUNTIME_SCRATCH_KEY = "multi_tool_agent_dag_runtime"

    def action_handler(self, message: Message) -> Any:
        return super().action_handler(message)

    def _validate_list_action_and_action_input(
        self, result_dict: dict, action_list: list, allowed_actions: set[str]
    ) -> None:
        """
        Validate multi-tool list action payloads strictly.

        Accepted shapes:
        - action: list[{"tools": list[str]}]
        - action: list[list[str]]

        Rules:
        - Every declared callable must be in this agent's allowed action set (tools + allowed_nodes).
        - `return_control` / `done` / `execute_dag` are not valid entries inside sequences.
        - Runtime decides how to execute each callable kind (tool, agent, control node).
        - Empty action list is only allowed when `exit=true`.
        - For list actions, action_input must be absent or a list.
        """
        should_exit = bool(result_dict.get("exit", False))
        action_input = result_dict.get("action_input")
        if action_input is not None and not isinstance(action_input, list):
            logger.error(
                "[%s] For list action payload, action_input must be a list or null. Got: %r",
                self.name,
                type(action_input),
            )
            raise ValueError(f"[{self.name}] action_input must be a list when action is a list.")

        def _validate_callable_name(raw_callable: Any) -> None:
            if not isinstance(raw_callable, str) or not raw_callable.strip():
                logger.error("[%s] Invalid callable entry in action list: %r", self.name, raw_callable)
                raise ValueError(f"[{self.name}] Invalid callable entry in action list.")
            callable_name = raw_callable.strip()
            if callable_name in {"return_control", "done", "execute_dag"}:
                logger.error("[%s] Invalid pseudo-callable '%s' inside action list.", self.name, callable_name)
                raise ValueError(f"[{self.name}] Invalid pseudo-callable '{callable_name}' in action list.")
            if callable_name not in allowed_actions:
                logger.error(
                    "[%s] Callable '%s' is not allowed for this agent. Allowed actions: %s",
                    self.name,
                    callable_name,
                    sorted(allowed_actions),
                )
                raise ValueError(f"[{self.name}] Callable '{callable_name}' is not allowed.")

        if len(action_list) == 0:
            if should_exit:
                return
            logger.error("[%s] Empty action list is only valid when exit=true.", self.name)
            raise ValueError(f"[{self.name}] Empty action list requires exit=true.")

        is_obj_schema = all(isinstance(seq, dict) for seq in action_list)
        is_list_schema = all(isinstance(seq, list) for seq in action_list)
        if not is_obj_schema and not is_list_schema:
            logger.error(
                "[%s] Invalid list action schema. Expected list[dict] or list[list], got: %r",
                self.name,
                action_list,
            )
            raise ValueError(f"[{self.name}] Invalid action list schema.")

        if is_obj_schema:
            for idx, seq_obj in enumerate(action_list, start=1):
                tools = seq_obj.get("tools")
                if not isinstance(tools, list):
                    logger.error("[%s] action[%s].tools must be a list. Got: %r", self.name, idx, type(tools))
                    raise ValueError(f"[{self.name}] action[{idx}].tools must be a list.")
                if len(tools) == 0:
                    logger.error("[%s] action[%s].tools is empty.", self.name, idx)
                    raise ValueError(f"[{self.name}] action[{idx}].tools cannot be empty.")
                for tool in tools:
                    _validate_callable_name(tool)
            return

        # Compact list[list[str]] schema
        for idx, seq in enumerate(action_list, start=1):
            if len(seq) == 0:
                logger.error("[%s] action[%s] is an empty tool sequence.", self.name, idx)
                raise ValueError(f"[{self.name}] action[{idx}] cannot be an empty sequence.")
            for tool in seq:
                _validate_callable_name(tool)

    def _handle_flow_control(self, result_dict: dict):
        if not isinstance(result_dict, dict):
            return super()._handle_flow_control(result_dict)

        action = result_dict.get("action")
        if not isinstance(action, list):
            return super()._handle_flow_control(result_dict)

        sequence, sequence_inputs = self._extract_compiled_sequence(result_dict)
        should_exit = bool(result_dict.get("exit", False))

        if should_exit and not sequence:
            out = {
                "ok": True,
                "task_done": True,
                "action": "return_control",
                "plan_summary": result_dict.get("summary") or result_dict.get("plan_summary") or "",
                "raw_plan": result_dict,
            }
            self.blackboard.update_state_value("result", out)
            self.blackboard.update_state_value("last_agent", f"{self.name}_return_control")
            self.blackboard.update_state_value("next_agent", None)
            return

        if not sequence:
            out = {
                "ok": False,
                "error": "Planner returned non-exit action but no executable sequence.",
                "raw_plan": result_dict,
            }
            self.blackboard.update_state_value("result", out)
            self.blackboard.update_state_value("last_agent", f"{self.name}_return_control")
            self.blackboard.update_state_value("next_agent", None)
            return

        if should_exit:
            logger.info(
                "[%s] Planner returned exit=true with executable DAG; executing DAG before exit.",
                self.name,
            )

        state = {
            "planner_name": self.name,
            "sequence": sequence,
            "sequence_inputs": sequence_inputs,
            "max_parallel": 5,
            "waiting_for_result": False,
            "current_step_id": None,
            "results": {},
            "order": [],
            "status_by_step": {},
            "allowed_tools": [],
        }
        for i, raw_step in enumerate(sequence, start=1):
            step = raw_step if isinstance(raw_step, dict) else {}
            sid = str(step.get("id") or f"step_{i}")
            state["status_by_step"][sid] = "pending"

        self.blackboard.add_msg(
            Message(
                data_type="tool_request",
                sub_data_type=["dag_tool_call"],
                sender=self.name,
                receiver="dag_manager_control_node",
                content=self._format_dag_tool_request(sequence, sequence_inputs),
            )
        )
        self._set_dag_runtime(state)
        self.blackboard.update_state_value("last_agent", f"{self.name}_execute_dag")
        self.blackboard.update_state_value("next_agent", None)

    def _format_dag_tool_request(
        self,
        sequence: List[Dict[str, Any]],
        sequence_inputs: Dict[str, Dict[str, str]],
    ) -> str:
        groups: Dict[str, List[str]] = {}
        for raw in sequence:
            step = raw if isinstance(raw, dict) else {}
            seq_id = str(step.get("_sequence_id") or "").strip()
            tool = str(step.get("tool_name") or "").strip()
            if not seq_id or not tool:
                continue
            groups.setdefault(seq_id, []).append(tool)
        if not groups:
            return "LAST TOOL CALL: DAG (empty sequence)."
        lines: List[str] = ["LAST TOOL CALL: DAG execute"]
        for seq_id in sorted(groups.keys()):
            initial_input = ""
            seq_map = sequence_inputs.get(seq_id) if isinstance(sequence_inputs, dict) else None
            if isinstance(seq_map, dict):
                initial_input = str(seq_map.get("initial_input") or "").strip()
            chain = " -> ".join(groups.get(seq_id, []))
            lines.append(f"- {seq_id}: {chain}")
            if initial_input:
                lines.append(f"  input: {initial_input}")
        return "\n".join(lines)

    def _set_dag_runtime(self, state: Dict[str, Any] | None) -> None:
        set_scratch(self.blackboard, self.DAG_RUNTIME_SCRATCH_KEY, state if isinstance(state, dict) else None)

    def _extract_compiled_sequence(self, payload: Dict[str, Any]) -> tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]:
        if not isinstance(payload, dict):
            return [], {}

        # Compatibility path: precompiled top-level `sequence`.
        precompiled_sequence = payload.get("sequence")
        if isinstance(precompiled_sequence, list):
            return [s for s in precompiled_sequence if isinstance(s, dict)], {}

        # New schema support:
        # - action: List[{"tools": [toolA, toolB, ...]}]
        # - action_input: List[str] aligned to each action sequence.
        action_val = payload.get("action")
        action_input_val = payload.get("action_input")
        if isinstance(action_val, list) and all(isinstance(seq, dict) and isinstance(seq.get("tools"), list) for seq in action_val):
            compiled: List[Dict[str, Any]] = []
            sequence_inputs: Dict[str, Dict[str, str]] = {}
            initial_inputs = action_input_val if isinstance(action_input_val, list) else []

            for i, seq_obj in enumerate(action_val, start=1):
                tools = [
                    str(t).strip()
                    for t in (seq_obj.get("tools") or [])
                    if isinstance(t, str) and str(t).strip()
                ]
                tools = [t for t in tools if t not in {"execute_dag", "return_control"}]
                if not tools:
                    continue
                seq_id = f"seq{i}"
                first_input = ""
                if i - 1 < len(initial_inputs) and isinstance(initial_inputs[i - 1], str):
                    first_input = str(initial_inputs[i - 1])
                sequence_inputs[seq_id] = {"initial_input": first_input}

                prev_id = None
                for j, tool_name in enumerate(tools, start=1):
                    step_id = f"{seq_id}::step{j}"
                    step: Dict[str, Any] = {
                        "id": step_id,
                        "tool_name": tool_name,
                        "depends_on": [prev_id] if isinstance(prev_id, str) else [],
                        "_sequence_id": seq_id,
                    }
                    if j == 1 and first_input.strip():
                        step["action_input"] = first_input
                    compiled.append(step)
                    prev_id = step_id
            return compiled, sequence_inputs

        # Compatibility path for compact format:
        # - action: list[list[str]]
        # - action_input: list[str] aligned to each outer sequence.
        if isinstance(action_val, list) and all(isinstance(seq, list) for seq in action_val):
            compiled: List[Dict[str, Any]] = []
            sequence_inputs: Dict[str, Dict[str, str]] = {}
            initial_inputs = action_input_val if isinstance(action_input_val, list) else []

            for i, seq in enumerate(action_val, start=1):
                tools = [str(t).strip() for t in seq if isinstance(t, str) and str(t).strip()]
                tools = [t for t in tools if t not in {"execute_dag", "return_control"}]
                if not tools:
                    continue
                seq_id = f"seq{i}"
                first_input = ""
                if i - 1 < len(initial_inputs) and isinstance(initial_inputs[i - 1], str):
                    first_input = str(initial_inputs[i - 1])
                sequence_inputs[seq_id] = {"initial_input": first_input}

                prev_id = None
                for j, tool_name in enumerate(tools, start=1):
                    step_id = f"{seq_id}::step{j}"
                    step: Dict[str, Any] = {
                        "id": step_id,
                        "tool_name": tool_name,
                        "depends_on": [prev_id] if isinstance(prev_id, str) else [],
                        "_sequence_id": seq_id,
                    }
                    if j == 1 and first_input.strip():
                        step["action_input"] = first_input
                    compiled.append(step)
                    prev_id = step_id
            return compiled, sequence_inputs

        return [], {}

