from pydantic import BaseModel, Field
from typing import Optional, List


class kg_describe_node_args(BaseModel):
    """Arguments for kg_describe_node tool."""
    node_ids: List[str] = Field(..., description="List of node UUIDs to describe in detail")
    max_edges: Optional[int] = Field(default=5, description="Maximum number of edges to return per node (default: 5)")
    include_raw: Optional[bool] = Field(default=False, description="Whether to return raw JSON data instead of formatted text (default: false)")

class kg_describe_node_arguments(BaseModel):
    tool_name: str
    arguments: kg_describe_node_args

