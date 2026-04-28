from typing import List
from pydantic import BaseModel, Field


class ToolArgumentPair(BaseModel):
    key: str = Field(description="Argument key.")
    value: str = Field(description="Argument value as string.")

class AgentForm(BaseModel):
    tool_argument_pairs: List[ToolArgumentPair]
