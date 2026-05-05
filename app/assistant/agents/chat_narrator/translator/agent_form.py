from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    """Output for chat_narrator::translator.

    Translates a planner's internal ``what_i_am_thinking`` into a single
    short, natural chat sentence in the worker's voice. Used live during
    long-running sub-manager calls to keep the user informed.
    """
    narration: str = Field(
        description=(
            "One short, present-tense sentence (max ~120 chars) the worker "
            "would naturally say to the user about what they're doing right "
            "now. Skip planner scaffolding (checklist, action_count, "
            "critic, return_control). Skip meta-talk about strategy. Just "
            "describe the concrete action or finding in plain language. "
            "If there's nothing concrete to say, return an empty string."
        ),
    )
