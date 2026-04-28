from typing import List

from pydantic import BaseModel


class AgentForm(BaseModel):
    category: str
    labels: List[str]
