from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class invoke_agent_args(BaseModel):
    agent_name: str = Field(..., description="Name of the registered agent to invoke.")
    agent_input: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Dict of key-value pairs unpacked onto the agent's blackboard. Keys map to Jinja2 template variables.",
    )


class invoke_agent_arguments(BaseModel):
    tool_name: str
    arguments: invoke_agent_args
