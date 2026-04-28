from __future__ import annotations

from app.assistant.agent_classes.PlaywrightAgent import PlaywrightAgent
from app.assistant.utils.history_formatting import format_recent_history


class PlaywrightCritic(PlaywrightAgent):
    """
    Playwright critic agent.
    """

    def build_recent_history(self, agent_messages):
        try:
            msgs = list(agent_messages or [])
        except Exception:
            msgs = agent_messages

        return format_recent_history(msgs)

