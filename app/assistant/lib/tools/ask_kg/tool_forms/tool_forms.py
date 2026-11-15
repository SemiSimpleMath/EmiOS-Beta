from typing import Optional

from pydantic import BaseModel, Field


class ask_kg_args(BaseModel):
    question: str = Field(..., description="Question to answer using node-local KG evidence")
    node: Optional[str] = Field(None, description="Base node name/title (or node id if known); omit to search the whole graph")


class ask_kg_arguments(BaseModel):
    tool_name: str
    arguments: ask_kg_args


ask_kg_arguments.model_rebuild()
