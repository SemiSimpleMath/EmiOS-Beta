from pydantic import BaseModel, Field
from typing import Optional


class AgentForm(BaseModel):
    decision: str
    target_id: str
    confidence: float
    reason: str
