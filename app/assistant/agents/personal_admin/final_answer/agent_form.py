from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    final_answer_answer: str = Field(
        default="",
        description="Short factual statement of the outcome. Neutral third-person, one or two sentences.",
    )
