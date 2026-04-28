from typing import List

from pydantic import BaseModel

class SequenceTools(BaseModel):
    tools: List[str]

class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[str]
    progress: List[str]
    plan: str
    action: List[SequenceTools]
    action_input: List[str]
    exit: bool

