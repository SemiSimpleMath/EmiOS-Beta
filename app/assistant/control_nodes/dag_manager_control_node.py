from __future__ import annotations

import json
import importlib
from concurrent.futures import as_completed
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import get_scratch, set_scratch
from app.assistant.utils.pydantic_classes import Message
from app.assistant.runtime import MonitoredThreadPoolExecutor

logger = get_logger(__name__)


class DagManagerControlNode(ControlNode):
    STATE_KEY = "multi_tool_agent_dag_runtime"

    def action_handler(self, message):
        state = get_scratch(self.blackboard, self.STATE_KEY, None)
        if not isinstance(state, dict):
            self._publish_result({"ok": False, "error": "Missing DAG execution state."}, None)
            return

        sequence = state.get("sequence") if isinstance(state.get("sequence"), list) else []
        if not sequence:
            self._publish_result({"ok": False, "error": "Empty DAG sequence."}, state)
            return

        grouped = self._group_by_sequence(sequence)
        if not grouped:
            self._publish_result({"ok": False, "error": "No executable sequences in DAG."}, state)
            return

        seq_inputs = state.get("sequence_inputs") if isinstance(state.get("sequence_inputs"), dict) else {}
        task_text = str(self.blackboard.get_state_value("task", "") or "")

        # Spawn per-sequence executors in parallel.
        max_parallel = int(state.get("max_parallel") or len(grouped))
        if max_parallel <= 0:
            max_parallel = len(grouped)
        workers = max(1, min(max_parallel, len(grouped)))

        dag_exec_module = importlib.import_module("app.assistant.control_nodes.dag_executor_node")
        dag_exec_cls = getattr(dag_exec_module, "DagExecutorNode")
        exec_node = dag_exec_cls(
            name="dag_executor_node",
            blackboard=self.blackboard,
            agent_registry=self.agent_registry,
            tool_registry=self.tool_registry,
        )

        sequence_results: Dict[str, Any] = {}
        merged_steps: Dict[str, Any] = {}
        merged_order: List[str] = []
        terminal_outputs: Dict[str, Any] = {}
        any_error = False

        with MonitoredThreadPoolExecutor(
            name=f"dag-manager-{self.name}",
            owner=self.name,
            max_workers=workers,
            metadata={"component": "dag_manager_control_node"},
        ) as pool:
            future_by_seq = {}
            for seq_id, steps in grouped.items():
                initial_input = ""
                seq_map = seq_inputs.get(seq_id) if isinstance(seq_inputs, dict) else None
                if isinstance(seq_map, dict):
                    initial_input = str(seq_map.get("initial_input") or "")
                fut = pool.submit(
                    exec_node.execute_sequence_sync,
                    sequence_id=seq_id,
                    steps=steps,
                    initial_input=initial_input,
                    task_text=task_text,
                )
                future_by_seq[fut] = seq_id

            for fut in as_completed(future_by_seq):
                seq_id = future_by_seq[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {
                        "ok": False,
                        "sequence_id": seq_id,
                        "error": f"sequence executor crashed: {e}",
                        "steps": {},
                        "order": [],
                    }
                sequence_results[seq_id] = res
                if not bool(res.get("ok", False)):
                    any_error = True
                steps = res.get("steps") if isinstance(res.get("steps"), dict) else {}
                for sid, sres in steps.items():
                    merged_steps[str(sid)] = sres
                ords = res.get("order") if isinstance(res.get("order"), list) else []
                merged_order.extend([str(x) for x in ords if isinstance(x, str)])
                terminal_step_id = str(res.get("terminal_step_id") or "").strip()
                terminal_output = res.get("terminal_output")
                if terminal_step_id and isinstance(terminal_output, dict):
                    terminal_outputs[terminal_step_id] = terminal_output

        final_result = {
            "ok": not any_error,
            "task_done": not any_error,
            "terminal_outputs": terminal_outputs,
            "terminal_outputs_text": exec_node._terminal_outputs_text(terminal_outputs),
            "steps": merged_steps,
            "order": merged_order,
            "sequence_results": sequence_results,
        }
        self._publish_result(final_result, state)

    def _group_by_sequence(self, sequence: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for raw in sequence:
            step = raw if isinstance(raw, dict) else {}
            seq_id = str(step.get("_sequence_id") or "").strip()
            if not seq_id:
                continue
            if seq_id not in out:
                out[seq_id] = []
            out[seq_id].append(step)
        return out

    def _publish_result(self, result: Dict[str, Any], state: Dict[str, Any] | None) -> None:
        planner_name = state.get("planner_name") if isinstance(state, dict) else None
        self.blackboard.update_state_value("result", result)
        set_scratch(self.blackboard, self.STATE_KEY, None)

        summary_text = self._format_result_summary(result)
        content = f"TOOL RESULTS OF LAST TOOL CALL:\n{summary_text}"
        msg_data = {"result_type": "dag_result", "data": result}
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
        self.blackboard.add_msg(
            Message(
                data_type="tool_result_summary",
                sub_data_type=["result_summary", "dag_result_summary"],
                sender=self.name,
                receiver=planner_name if isinstance(planner_name, str) else "Blackboard",
                content=summary_text,
            )
        )

        if isinstance(planner_name, str) and planner_name.strip():
            set_scratch(self.blackboard, f"{planner_name}::dag_result", result)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("next_agent", planner_name)
        else:
            self.blackboard.update_state_value("last_agent", f"{self.name}_return_control")
            self.blackboard.update_state_value("next_agent", None)
        logger.info("[%s] final DAG manager result: %s", self.name, json.dumps(result, ensure_ascii=False))

    def _format_result_summary(self, result: Dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return "No DAG result payload."
        seq_results = result.get("sequence_results") if isinstance(result.get("sequence_results"), dict) else {}
        if not seq_results:
            terminal_text = str(result.get("terminal_outputs_text") or "").strip()
            return terminal_text or "No terminal outputs."

        lines: List[str] = []
        for seq_id in sorted(seq_results.keys()):
            seq = seq_results.get(seq_id) if isinstance(seq_results.get(seq_id), dict) else {}
            terminal = seq.get("terminal_output") if isinstance(seq.get("terminal_output"), dict) else {}
            ok = bool(seq.get("ok", False))
            content = str(terminal.get("content") or "").strip()
            if not content:
                content = str(seq.get("error") or "").strip()
            status = "ok" if ok else "failed"
            line = f"- {seq_id} ({status}): {content}" if content else f"- {seq_id} ({status})"
            lines.append(line)
        return "\n".join(lines).strip() or "No sequence summaries."
