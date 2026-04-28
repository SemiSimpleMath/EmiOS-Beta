"""
Pydantic schemas for kg_create_edge tool
"""
from pydantic import BaseModel
from typing import Dict, Any, Union

class kg_create_edge_args(BaseModel):
    """Input schema for kg_create_edge tool."""
    source_id: str
    target_id: str
    relationship_type: str
    sentence: Union[str, None]
    confidence: Union[float, None]
    importance: Union[float, None]

class kg_create_edge_arguments(BaseModel):
    """Tool wrapper for kg_create_edge."""
    tool_name: str
    arguments: kg_create_edge_args
