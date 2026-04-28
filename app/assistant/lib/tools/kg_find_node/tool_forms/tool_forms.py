from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union


class kg_find_node_args(BaseModel):
    text: str
    threshold: float
    k: int
    node_id: str
    edges_k: int
    node_types: List[str]
    start_date: str
    end_date: str
    max_hops: int
    taxonomy_paths: List[str] = Field(default_factory=list)


class kg_find_node_arguments(BaseModel):
    tool_name: str
    arguments: kg_find_node_args

