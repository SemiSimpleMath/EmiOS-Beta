from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    final_answer_answer: str = Field(
        description="The user-facing message. Detail and format calibrated to the task: a simple action gets one sentence; a research task can use headings, bullets, inline links.",
    )
