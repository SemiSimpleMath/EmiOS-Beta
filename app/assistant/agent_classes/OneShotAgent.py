from __future__ import annotations

from app.assistant.agent_classes.Agent import Agent
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class OneShotAgent(Agent):
    """
    Lightweight agent used by one_shot_tool_runner.
    Returns the structured output dict without flow-control side effects.
    """

    def process_llm_result(self, result):
        self._maybe_print_llm_result(result)
        if isinstance(result, str):
            logger.error(f"[{self.name}] Expected dict, got string: {result}")
            raise ValueError(f"[{self.name}] Expected dict from LLM, got string.")
        if not isinstance(result, dict):
            logger.error(f"[{self.name}] Expected dict, got {type(result)}")
            raise TypeError(f"[{self.name}] Expected dict from LLM, got {type(result)}.")
        return result
