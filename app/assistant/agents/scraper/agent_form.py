from typing import List, Dict

from pydantic import BaseModel
class KeyValue(BaseModel):
    url: str
    description: str
class AgentForm(BaseModel):
    content: str
    links: List[KeyValue]
