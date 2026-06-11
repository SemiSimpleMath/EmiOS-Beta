from __future__ import annotations

import json
from typing import Any

from app.assistant.control_nodes.control_node import ControlNode


class TaskCompileFinalOutputNode(ControlNode):
    """
    Build final task-compile response payload from blackboard state.
    """

    def action_handler(self, message):
        _ = message
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()
        previous_agent = self._required_str(cfg, "previous_agent")
        next_agent = self._required_str(cfg, "next_agent")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != previous_agent:
            raise ValueError(
                f"[{self.name}] expected last_agent={previous_agent} before finalization, got: {last_agent!r}"
            )

        compiled_task = self.blackboard.get_state_value("compiled_task", None)
        if not isinstance(compiled_task, dict):
            raise TypeError(f"[{self.name}] compiled_task must be dict, got {type(compiled_task)}.")

        logic_tree = self.blackboard.get_state_value("logic_tree", None)
        if not isinstance(logic_tree, dict):
            raise TypeError(f"[{self.name}] logic_tree must be dict, got {type(logic_tree)}.")

        phase_i_atoms = self.blackboard.get_state_value("phase_i_atoms", None)
        if not isinstance(phase_i_atoms, dict):
            raise TypeError(f"[{self.name}] phase_i_atoms must be dict, got {type(phase_i_atoms)}.")

        data_list: list[dict[str, Any]] = [
            {
                "data_type": "compiled_task",
                "key": "compiled_task",
                "value": json.dumps(compiled_task, ensure_ascii=False),
            },
            {
                "data_type": "logic_tree",
                "key": "logic_tree",
                "value": json.dumps(logic_tree, ensure_ascii=False),
            },
            {
                "data_type": "phase_i_atoms",
                "key": "phase_i_atoms",
                "value": json.dumps(phase_i_atoms, ensure_ascii=False),
            },
        ]

        compiled_task_file = self.blackboard.get_state_value("compiled_task_file", None)
        if isinstance(compiled_task_file, str) and compiled_task_file.strip():
            data_list.append(
                {
                    "data_type": "compiled_task_file",
                    "key": "compiled_task_file",
                    "value": compiled_task_file.strip(),
                }
            )

        self.blackboard.update_state_value("final_answer_data_list", data_list)
        self.blackboard.update_state_value("last_agent", self.name)
        self.blackboard.update_state_value("next_agent", next_agent)

    def _cfg(self) -> dict:
        return self._node_cfg()
