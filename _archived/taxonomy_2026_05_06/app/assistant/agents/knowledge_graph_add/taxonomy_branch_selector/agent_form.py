from typing import List
from pydantic import BaseModel

class BranchSelection(BaseModel):
    label: str
    relevance: float

class AgentForm(BaseModel):
    selected_branches: List[BranchSelection]
    confidence: float
    reasoning: str
