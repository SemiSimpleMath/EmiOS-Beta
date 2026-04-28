from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    chat_response: str = Field(default="", description="Planning-mode chat reply to the user.")
    planning_done_tf: bool = Field(
        default=False,
        description="True only when the user explicitly says planning is done.",
    )
    plan_summary: str = Field(
        default="",
        description="Short summary of agreed plan when planning_done_tf is True.",
    )
