from typing import Literal
from pydantic import BaseModel, Field

class Pair(BaseModel):
    focal_id: str
    candidate_id: str
    reason: str

class AgentForm(BaseModel):
    pairs: list[Pair]
    reasoning: str
