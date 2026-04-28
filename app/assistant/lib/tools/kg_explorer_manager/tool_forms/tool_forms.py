from pydantic import BaseModel, Field
from typing import Union, Dict, Any


class kg_explorer_manager_args(BaseModel):
    """Arguments for the kg_explorer_manager tool call."""
    task: str
    information: str



class kg_explorer_manager_arguments(BaseModel):
    """Tool message schema for kg_explorer_manager tool."""
    tool_name: str
    arguments: kg_explorer_manager_args
