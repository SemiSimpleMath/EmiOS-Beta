from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    chat_response: str = Field(
        default="",
        description=(
            "User-visible reply. Required when handoff_tf=False. "
            "Also required when handoff_tf=True — write a genuine natural acknowledgment "
            "telling the user what you are about to do, e.g. 'On it — I'll add that to the calendar.'"
        ),
    )
    no_op_tf: bool = Field(
        default=False,
        description="True when no reply and no switchboard handoff are required for this turn.",
    )
    handoff_tf: bool = Field(
        default=False,
        description="False when chat response is sufficient and the manager should exit without handing off to switchboard.",
    )
    switchboard_task: str = Field(default="", description="Task for switchboard when handoff_tf is True.")
    switchboard_information: str = Field(default="", description="Supporting information for switchboard when handoff_tf is True.")
    dayflow_delegate_tf: bool = Field(
        default=False,
        description=(
            "Set true when the request involves waiting for future events, monitoring conditions over time, "
            "multi-step conditional logic, or anything that cannot complete in a single synchronous tool call. "
            "Mutually exclusive with handoff_tf and no_op_tf."
        ),
    )
    dayflow_task_description: str = Field(
        default="",
        description="Concise description of what to monitor or execute. Required when dayflow_delegate_tf is True.",
    )
