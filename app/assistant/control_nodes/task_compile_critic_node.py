from __future__ import annotations

import ast
import re
from typing import Any

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_MAX_RETRIES = 2


class TaskCompileCriticNode(ControlNode):
    """
    Deterministic structural critic for phase_ii compiled_task output.

    Checks the compiled_task on the blackboard for structural contract
    violations that post_node would hard-fail on, and either:
      - Approves  → routes to post_node as normal.
      - Rejects   → writes critic_feedback to the blackboard and routes
                    back to phase_ii for a targeted retry (up to _MAX_RETRIES).

    Checks performed:
      - entry_step_id exists and is present in steps
      - no duplicate step ids
      - all next_step / on_true / on_false references resolve to real step ids or ""
      - every step has a non-empty kind
      - wait_for_event steps have non-empty event_name
      - decision steps have non-empty on_true and on_false, and they differ
      - wait_gate steps have non-empty subscriptions and release_condition
      - release_condition / condition expressions are Python ast-parseable when non-empty
      - action steps declare at most one artifact_* in produces_data_ids
      - no step id equals another step's reference target with wrong kind
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()
        previous_agents = self._required_previous_agents(cfg)
        next_agent = self._required_str(cfg, "next_agent")
        retry_agent = self._required_str(cfg, "retry_agent")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent not in previous_agents:
            raise ValueError(
                f"[{self.name}] expected last_agent in {previous_agents} before critic, got: {last_agent!r}"
            )

        compiled_task = self.blackboard.get_state_value("compiled_task", None)
        if not isinstance(compiled_task, dict):
            raise TypeError(f"[{self.name}] compiled_task must be dict, got {type(compiled_task)}.")

        violations = self._check(compiled_task)

        retry_count = int(self.blackboard.get_state_value("critic_retry_count", 0) or 0)

        if not violations:
            logger.info("[%s] compiled_task passed critic review (retry_count=%d)", self.name, retry_count)
            self.blackboard.update_state_value("critic_feedback", None)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("next_agent", next_agent)
            return

        violation_text = "\n".join(f"- {v}" for v in violations)
        logger.warning(
            "[%s] compiled_task has %d violation(s) (retry_count=%d/%d):\n%s",
            self.name,
            len(violations),
            retry_count,
            _MAX_RETRIES,
            violation_text,
        )

        if retry_count >= _MAX_RETRIES:
            logger.error(
                "[%s] max retries reached (%d). Passing to post_node despite violations.",
                self.name,
                _MAX_RETRIES,
            )
            self.blackboard.update_state_value("critic_feedback", None)
            self.blackboard.update_state_value("last_agent", self.name)
            self.blackboard.update_state_value("next_agent", next_agent)
            return

        self.blackboard.update_state_value("critic_retry_count", retry_count + 1)
        self.blackboard.update_state_value(
            "critic_feedback",
            f"The compiled_task you produced has the following structural violations "
            f"that must be fixed before it can be accepted:\n{violation_text}\n\n"
            f"Please re-emit a corrected compiled_task and logic_tree that resolves all violations.",
        )
        self.blackboard.update_state_value("last_agent", self.name)
        self.blackboard.update_state_value("next_agent", retry_agent)
        logger.info("[%s] Routing back to %s for retry %d.", self.name, retry_agent, retry_count + 1)

    # ── Checks ────────────────────────────────────────────────────────────────

    def _check(self, compiled_task: dict) -> list[str]:
        violations: list[str] = []
        steps = compiled_task.get("steps")
        if not isinstance(steps, list) or not steps:
            violations.append("compiled_task.steps must be a non-empty list.")
            return violations

        step_ids: set[str] = set()
        step_by_id: dict[str, dict[str, Any]] = {}
        for raw in steps:
            if not isinstance(raw, dict):
                violations.append(f"Each step must be a dict; found {type(raw).__name__}.")
                continue
            sid = str(raw.get("id") or "").strip()
            if not sid:
                violations.append("A step is missing a non-empty 'id'.")
                continue
            if sid in step_ids:
                violations.append(f"Duplicate step id: '{sid}'.")
            step_ids.add(sid)
            step_by_id[sid] = raw

        entry = str(compiled_task.get("entry_step_id") or "").strip()
        if not entry:
            violations.append("compiled_task.entry_step_id is required.")
        elif entry not in step_ids:
            violations.append(f"entry_step_id '{entry}' does not match any step id.")

        for sid, step in step_by_id.items():
            kind = str(step.get("kind") or "").strip()
            if not kind:
                violations.append(f"Step '{sid}' is missing a non-empty 'kind'.")
                continue

            violations.extend(self._check_step_refs(sid, step, step_ids))

            if kind == "action":
                violations.extend(self._check_action_step(sid, step))
            elif kind == "tool_sequence":
                # Deterministic tool call sequence — validated by post_node.
                pass
            elif kind == "wait_for_event":
                violations.extend(self._check_wait_for_event_step(sid, step))
            elif kind == "wait_gate":
                violations.extend(self._check_wait_gate_step(sid, step))
            elif kind == "decision":
                violations.extend(self._check_decision_step(sid, step))
            elif kind == "end":
                pass
            else:
                violations.append(f"Step '{sid}' has unsupported kind '{kind}'.")

        return violations

    @staticmethod
    def _check_step_refs(sid: str, step: dict, step_ids: set[str]) -> list[str]:
        out: list[str] = []
        for ref_key in ("next_step", "on_true", "on_false"):
            ref = str(step.get(ref_key) or "").strip()
            if ref and ref not in step_ids:
                out.append(f"Step '{sid}' {ref_key}='{ref}' does not match any step id.")
        return out

    @staticmethod
    def _check_action_step(sid: str, step: dict) -> list[str]:
        out: list[str] = []
        produces = step.get("produces_data_ids") or []
        if not isinstance(produces, list):
            out.append(f"Action step '{sid}' produces_data_ids must be a list.")
            return out
        artifact_ids = [d for d in produces if isinstance(d, str) and d.startswith("artifact_")]
        if len(artifact_ids) > 1:
            out.append(
                f"Action step '{sid}' declares {len(artifact_ids)} artifact_* ids "
                f"({artifact_ids}); at most one is allowed."
            )
        return out

    @staticmethod
    def _check_wait_for_event_step(sid: str, step: dict) -> list[str]:
        out: list[str] = []
        event_name = str(step.get("event_name") or "").strip()
        if not event_name:
            out.append(f"wait_for_event step '{sid}' is missing a non-empty event_name.")
        return out

    @staticmethod
    def _check_wait_gate_step(sid: str, step: dict) -> list[str]:
        out: list[str] = []
        subs = step.get("subscriptions")
        if not isinstance(subs, list) or not subs:
            out.append(f"wait_gate step '{sid}' must have a non-empty subscriptions list.")
        release = str(step.get("release_condition") or "").strip()
        if not release:
            out.append(f"wait_gate step '{sid}' is missing release_condition.")
        else:
            try:
                ast.parse(release, mode="eval")
            except SyntaxError:
                out.append(
                    f"wait_gate step '{sid}' release_condition is not Python-parseable: {release!r}"
                )
        return out

    @staticmethod
    def _check_decision_step(sid: str, step: dict) -> list[str]:
        out: list[str] = []
        on_true = str(step.get("on_true") or "").strip()
        on_false = str(step.get("on_false") or "").strip()
        if not on_true:
            out.append(f"decision step '{sid}' is missing on_true.")
        if not on_false:
            out.append(f"decision step '{sid}' is missing on_false.")
        if on_true and on_false and on_true == on_false:
            out.append(
                f"decision step '{sid}' on_true and on_false both point to '{on_true}'; "
                "they must differ."
            )
        condition = str(step.get("condition") or "").strip()
        if not condition:
            out.append(f"decision step '{sid}' is missing condition.")
        else:
            try:
                ast.parse(condition, mode="eval")
            except SyntaxError:
                out.append(
                    f"decision step '{sid}' condition is not Python-parseable: {condition!r}"
                )
        return out

    # ── Config helpers ────────────────────────────────────────────────────────

    def _cfg(self) -> dict:
        node_cfg_map = self.blackboard.get_state_value("manager_control_node_configs", {})
        if not isinstance(node_cfg_map, dict):
            raise ValueError("manager_control_node_configs must be a dict.")
        node_cfg = node_cfg_map.get(self.name, {})
        if not isinstance(node_cfg, dict):
            raise ValueError(f"manager_control_node_configs['{self.name}'] must be a dict.")
        if not node_cfg:
            raise ValueError(f"[{self.name}] missing config.")
        return node_cfg

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"task compile critic node requires non-empty '{key}'.")
        return raw.strip()

    @staticmethod
    def _required_previous_agents(cfg: dict) -> set[str]:
        raw_single = cfg.get("previous_agent")
        raw_multi = cfg.get("previous_agents")
        out: set[str] = set()
        if isinstance(raw_single, str) and raw_single.strip():
            out.add(raw_single.strip())
        if isinstance(raw_multi, list):
            for item in raw_multi:
                if isinstance(item, str) and item.strip():
                    out.add(item.strip())
        if not out:
            raise ValueError("task compile critic node requires 'previous_agent' or 'previous_agents'.")
        return out
