"""SteeringInjectNode — pops the steering inbox at start of each planner
iteration and writes the messages into the blackboard for the planner
to consume.

Sits between the loop tail (or initial entry) and the planner agent in a
sub-manager's state_map. Runs synchronously, in the manager's own thread,
so it always executes BEFORE the planner's LLM call — never mid-call.

Reads:
- ``_invocation_id`` from blackboard (set by ManagerInvoker on entry)

Writes:
- ``user_steering`` blackboard key — concatenated text of all popped
  steering messages, or "" if inbox is empty. Planner agent_form
  / system prompt should reference this key to incorporate the steering
  on its next plan step.
"""
from __future__ import annotations

from typing import List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class SteeringInjectNode(ControlNode):
    """Pop SteeringInbox messages for this invocation and stage on blackboard."""

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        invocation_id = str(
            self.blackboard.get_state_value("_invocation_id", "") or ""
        ).strip()
        if not invocation_id:
            # No invocation_id available — silently skip. Caller still
            # falls through to the planner via the default state_map edge.
            self.blackboard.update_state_value("user_steering", "")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        inbox = getattr(DI, "steering_inbox", None)
        if inbox is None:
            self.blackboard.update_state_value("user_steering", "")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        messages = inbox.pop_all(invocation_id)
        if not messages:
            self.blackboard.update_state_value("user_steering", "")
            self.blackboard.update_state_value("last_agent", self.name)
            return

        # Concatenate in posting order. Each line is one user steering
        # message; the planner's prompt formats them collectively.
        rendered_lines: List[str] = []
        for m in messages:
            text = (m.text or "").strip()
            if text:
                rendered_lines.append(f"- {text}")
        rendered = "\n".join(rendered_lines)
        self.blackboard.update_state_value("user_steering", rendered)
        logger.info(
            "[%s] injected %d steering message(s) for invocation %s",
            self.name, len(messages), invocation_id,
        )
        self.blackboard.update_state_value("last_agent", self.name)
