"""
Pydantic schemas for kg_delete_edge tool
"""
from pydantic import BaseModel

class kg_delete_edge_args(BaseModel):
    """Input schema for kg_delete_edge tool."""
    edge_id: str

class kg_delete_edge_arguments(BaseModel):
    """Tool wrapper for kg_delete_edge."""
    tool_name: str
    arguments: kg_delete_edge_args
