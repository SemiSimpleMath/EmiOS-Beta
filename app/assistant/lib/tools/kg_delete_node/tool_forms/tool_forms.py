"""
Pydantic schemas for kg_delete_node tool
"""
from pydantic import BaseModel
from typing import Union

class kg_delete_node_args(BaseModel):
    """Input schema for kg_delete_node tool."""
    node_id: str
    confirm: Union[bool, None]

class kg_delete_node_arguments(BaseModel):
    """Tool wrapper for kg_delete_node."""
    tool_name: str
    arguments: kg_delete_node_args
