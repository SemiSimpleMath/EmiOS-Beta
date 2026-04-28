from app.assistant.agent_classes.Agent import Agent
from app.assistant.utils.pipeline_state import ensure_pipeline_state, get_pending_tool


class PlaywrightAgent(Agent):
    """
    Agent variant that injects pipeline state keys (pending tool info).
    """

    PIPELINE_KEYS = {
        "selected_tool",
        "pending_tool_name",
        "tool_arguments",
        "pending_tool_arguments",
        "action_input",
        "pending_tool_calling_agent",
        "pipeline_stage",
    }

    def generate_injections_block(self, prompt_injections, message=None):
        context = super().generate_injections_block(prompt_injections, message)
        if not isinstance(prompt_injections, list):
            return context
        if not any(k in self.PIPELINE_KEYS for k in prompt_injections):
            return context

        ps = ensure_pipeline_state(self.blackboard)
        pending = get_pending_tool(self.blackboard) or {}
        for key in prompt_injections:
            if key not in self.PIPELINE_KEYS:
                continue
            if key in ("selected_tool", "pending_tool_name"):
                context[key] = pending.get("name")
            elif key in ("tool_arguments", "pending_tool_arguments"):
                context[key] = pending.get("arguments")
            elif key == "action_input":
                context[key] = pending.get("action_input")
            elif key == "pending_tool_calling_agent":
                context[key] = pending.get("calling_agent")
            elif key == "pipeline_stage":
                context[key] = ps.get("stage")
        return context
