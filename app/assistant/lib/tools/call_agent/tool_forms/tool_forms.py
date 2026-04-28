from pydantic import BaseModel
from typing import Optional, Dict, Any


class call_agent_args(BaseModel):
    agent_name: str
    task: Optional[str] = None
    inputs: Optional[Dict[str, Any]] = None


class call_agent_arguments(BaseModel):
    tool_name: str
    arguments: call_agent_args
