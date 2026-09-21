from pydantic import BaseModel, Field


class Candidate(BaseModel):
    belief_id: str
    reason: str


class AgentForm(BaseModel):
    candidates: list[Candidate]
    coverage_notes: str = Field(description="What was searched, uncertainty, and possible missing context.")
