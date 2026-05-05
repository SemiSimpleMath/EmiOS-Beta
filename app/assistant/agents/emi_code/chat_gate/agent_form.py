from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    chat_response: str = Field(
        default="",
        description=(
            "User-visible reply. Required when handoff_tf=False. "
            "When handoff_tf=True, write a short natural acknowledgment "
            "(e.g. 'Forwarding to Claude Code…')."
        ),
    )
    no_op_tf: bool = Field(
        default=False,
        description="True when no reply and no handoff are required for this turn.",
    )
    handoff_tf: bool = Field(
        default=False,
        description=(
            "True when the request is a clear coding-related task to send "
            "to the coding agent. False when the gate is asking the user "
            "to clarify, replying directly, or declining an off-topic request."
        ),
    )
    switchboard_task: str = Field(
        default="",
        description=(
            "Required when handoff_tf=True. The user's request, "
            "verbatim or lightly polished. Do not paraphrase or summarize "
            "— the coding agent reads this directly."
        ),
    )
    switchboard_information: str = Field(
        default="",
        description=(
            "Optional supporting context for the coding agent when "
            "handoff_tf=True. Use this to add anything the user mentioned "
            "in earlier turns that the coding agent should know but isn't "
            "in the latest message. Empty string allowed."
        ),
    )
