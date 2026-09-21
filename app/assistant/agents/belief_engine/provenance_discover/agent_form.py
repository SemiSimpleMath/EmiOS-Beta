from pydantic import BaseModel
class Candidate(BaseModel):
    belief_id: str
    candidate_id: str
    reason: str
class AgentForm(BaseModel):
    candidates: list[Candidate]
