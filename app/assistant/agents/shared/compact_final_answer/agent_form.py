from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    final_answer_task: str = Field(
        default="",
        description="What was the task.",
    )
    final_answer_answer: str = Field(
        description="Concise result. For simple tasks: one sentence. For complex tasks: a short paragraph.",
    )
    final_answer_no_op: bool = Field(
        default=False,
        description="True if nothing was done (no-op).",
    )
