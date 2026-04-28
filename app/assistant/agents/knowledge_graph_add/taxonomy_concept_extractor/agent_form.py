from pydantic import BaseModel, Field
from typing import List


class AgentForm(BaseModel):
    concepts: List[str] = Field(..., description="1-5 core concept keywords to search for in taxonomy", min_items=1, max_items=5)
    reasoning: str = Field(..., description="Brief explanation of why these concepts were chosen")

