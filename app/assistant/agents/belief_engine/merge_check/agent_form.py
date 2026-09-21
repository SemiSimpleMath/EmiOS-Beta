from pydantic import BaseModel
from typing import Literal

class AgentForm(BaseModel):
    verdict: Literal['approve', 'preserve_separately']
    reason: str
    changed_meanings: list[str]
    evidence_ids: list[str]
