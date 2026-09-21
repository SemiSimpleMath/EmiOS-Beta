from pydantic import BaseModel
from typing import Literal
class Attribution(BaseModel):
    source_id: str
    relation: Literal['support','contradict','qualify','unrelated','insufficient']
    reason: str
class AgentForm(BaseModel):
    attributions: list[Attribution]
