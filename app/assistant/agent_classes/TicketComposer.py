from __future__ import annotations

from app.assistant.agent_classes.Agent import Agent
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TicketComposer(Agent):
    """ticket_builder::composer. When its form refuses the answer (a choice label over 3 words, for one), it is asked
    once more with the refusal named; a second refusal raises. Until 2026-10-13 a refused answer left the manager
    with no composer output and the whole ask was lost: the question whether the owner raised fatigue at his
    physical was never sent for one label, "Help me follow up"."""

    def _execute_model(self, messages):
        from app.assistant.control_nodes.work_architect_node import _form_refusal
        from app.assistant.dayflow_orchestrator.work_context import render_view
        try:
            return super()._execute_model(messages)
        except RuntimeError as exc:
            refusal = _form_refusal(exc)
            if refusal is None:
                raise
            logger.warning("[%s] answer refused by its form, one correction: %s", self.name, refusal)
            correction = render_view("architect_refused", reason=refusal)
            return super()._execute_model(messages + [{"role": "user", "content": correction}])
