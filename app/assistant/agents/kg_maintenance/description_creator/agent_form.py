from typing import List

from pydantic import BaseModel


class AgentForm(BaseModel):
    reasoning: str
    new_description: str

