"""
Pydantic schemas for kg_create_node tool
"""
from pydantic import BaseModel
from typing import Dict, Any, List, Union

class kg_create_node_args(BaseModel):
    """Input schema for kg_create_node tool."""
    label: str
    semantic_label: Union[str, None]
    node_type: str
    description: Union[str, None]
    aliases: Union[List[str], None]
    category: Union[str, None]
    start_date: Union[str, None]
    end_date: Union[str, None]
    start_date_confidence: Union[str, None]
    end_date_confidence: Union[str, None]
    confidence: Union[float, None]
    importance: Union[float, None]

class kg_create_node_arguments(BaseModel):
    """Tool wrapper for kg_create_node."""
    tool_name: str
    arguments: kg_create_node_args
