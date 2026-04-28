from typing import List

from pydantic import BaseModel

class AgentForm(BaseModel):
    should_initiate: bool
    reason: str
    intent: str
    topics: List[str]
    starter_text: str