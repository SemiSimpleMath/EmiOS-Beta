from typing import Literal
from pydantic import BaseModel, Field

class AgentForm(BaseModel):
    belief_ids: list[str]
    reasoning: str
