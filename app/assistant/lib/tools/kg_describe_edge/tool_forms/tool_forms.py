from pydantic import BaseModel, Field
from typing import Optional

class kg_describe_edge_args(BaseModel):
    edge_id: str



class kg_describe_edge_arguments(BaseModel):
    tool_name: str
    arguments: kg_describe_edge_args

