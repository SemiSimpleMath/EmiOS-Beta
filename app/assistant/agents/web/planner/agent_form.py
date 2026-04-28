from typing import List
from pydantic import BaseModel

class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[str]
    progress: str
    plan: str
    action: str
    action_input: str