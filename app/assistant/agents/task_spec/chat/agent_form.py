from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    chat_response: str = Field(
        default="",
        description="Conversational reply to the user.",
    )
    update_spec_tf: bool = Field(
        default=False,
        description=(
            "Set True when this exchange contains information that should update the spec. "
            "A separate editor agent will read the conversation and apply the changes. "
            "Set False when just chatting, clarifying, or asking questions."
        ),
    )
    task_creation_done_tf: bool = Field(
        default=False,
        description=(
            "Set True only when the user explicitly says 'compile' or 'compile it'. "
            "This enriches the spec with technical details and compiles it into a runnable workflow."
        ),
    )
    compiled_task_id: str = Field(
        default="",
        description="Leave empty — populated by the system after compile.",
    )
