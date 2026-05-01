from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    chat_response: str = Field(
        default="",
        description=(
            "User-visible reply. Required when handoff_tf=False. "
            "When handoff_tf=True, write a short natural acknowledgment of what is about to run, "
            "e.g. 'On it — looking that up.'"
        ),
    )
    no_op_tf: bool = Field(
        default=False,
        description="True when no reply and no handoff are required for this turn.",
    )
    handoff_tf: bool = Field(
        default=False,
        description=(
            "True when the request is a clear, fully disambiguated investigation that should run "
            "the planner now. False when the gate is asking for clarification or replying directly."
        ),
    )
    switchboard_task: str = Field(
        default="",
        description=(
            "Required when handoff_tf=True. The disambiguated instruction to send to "
            "kg_dev_manager. Include concrete node ids when applicable."
        ),
    )
    switchboard_information: str = Field(
        default="",
        description="Supporting context for kg_dev_manager when handoff_tf=True. Empty string allowed.",
    )
