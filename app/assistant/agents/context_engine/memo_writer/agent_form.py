from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    situation_summary: str = Field(description="2-3 sentence summary of the key context.")
    suggested_questions: str = Field(description="Bullet list of 3-5 follow-up questions phrased naturally.")
