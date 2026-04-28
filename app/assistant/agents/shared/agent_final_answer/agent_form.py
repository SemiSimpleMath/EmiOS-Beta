from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    final_answer_answer: str = Field(
        description="The answer to the task. Data if data was requested, confirmation if action was performed, error details if failed.",
    )
    result_summary: str = Field(
        default="",
        description="One-line outcome (max ~150 chars).",
    )
