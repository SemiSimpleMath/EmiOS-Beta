from typing import List

from pydantic import BaseModel


class AgentForm(BaseModel):
    summary: str
    action_items: List[str]
    importance: int