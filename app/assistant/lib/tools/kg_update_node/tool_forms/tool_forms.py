"""
Pydantic schemas for kg_update_node tool
"""
from pydantic import BaseModel
from typing import Dict, Any, List, Union

class kg_update_node_args(BaseModel):
    """Input schema for kg_update_node tool."""
    node_id: str
    label: Union[str, None]
    semantic_label: Union[str, None]
    description: Union[str, None]
    aliases: Union[List[str], None]
    category: Union[str, None]
    start_date: Union[str, None]
    end_date: Union[str, None]
    start_date_confidence: Union[str, None]
    end_date_confidence: Union[str, None]
    valid_during: str

class kg_update_node_arguments(BaseModel):
    """Tool wrapper for kg_update_node."""
    reasoning: str
    tool_name: str
    arguments: kg_update_node_args
