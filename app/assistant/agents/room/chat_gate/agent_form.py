from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    chat_response: str = Field(default="", description="Direct room reply when no planner run is needed.")
    no_op_tf: bool = Field(
        default=False,
        description="True when no reply and no switchboard handoff are required for this turn.",
    )
    handoff_tf: bool = Field(
        default=False,
        description="True when chat response is not sufficient and the manager should invoke the switchboard to take over.",
    )
    switchboard_task: str = Field(default="", description="Task for switchboard when handoff_tf is True.")
    switchboard_information: str = Field(default="", description="Supporting information for switchboard when handoff_tf is True.")
