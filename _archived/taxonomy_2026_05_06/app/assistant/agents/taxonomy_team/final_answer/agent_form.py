from typing import List, Dict, Any
from pydantic import BaseModel

class NodeData(BaseModel):
    key: str
    value: str

class ReasoningStep(BaseModel):
    entity: str
    insight_gained: str
    supporting_data: List[NodeData]

class AgentForm(BaseModel):
    answer: str
    reasoning_path: List[ReasoningStep]
