from typing import List
from pydantic import BaseModel


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[str]
    found_information: List[str]
    plan: str
    action: str
    action_input: str