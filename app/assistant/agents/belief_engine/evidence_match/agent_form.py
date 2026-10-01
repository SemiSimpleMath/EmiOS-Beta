from typing import Literal
from pydantic import BaseModel, Field

class AgentForm(BaseModel):
    belief_keys: list[str] = Field(description="The belief_key of each relevant belief on this catalog page.")
    reasoning: str
