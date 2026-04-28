from typing import List, Dict

from pydantic import BaseModel
class KeyValue(BaseModel):
    url: str
    summary: str
class AgentForm(BaseModel):
    content: str
    links: List[KeyValue]
