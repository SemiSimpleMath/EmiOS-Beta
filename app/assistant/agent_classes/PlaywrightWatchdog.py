from __future__ import annotations

from app.assistant.agent_classes.Agent import Agent


class PlaywrightWatchdog(Agent):
    """
    Lightweight monitor agent for Playwright browsing runs.

    Key behavior:
    - Emits a small structured result that can set `next_agent` to `critic_capture_node`
      when the planner appears stuck (repeats, errors, snapshot/read loops).
    """

    def build_recent_history(self, agent_messages):
        try:
            msgs = list(agent_messages or [])
        except Exception:
            msgs = agent_messages

        return super().build_recent_history(msgs)

