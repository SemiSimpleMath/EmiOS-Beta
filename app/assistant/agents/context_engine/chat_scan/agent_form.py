from pydantic import BaseModel
from typing import List


class AgentForm(BaseModel):
    should_activate: bool
    seeds: List[str]
    reason: str
