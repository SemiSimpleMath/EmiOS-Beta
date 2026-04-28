from pydantic import BaseModel, Field


class ask_kg_args(BaseModel):
    question: str = Field(..., description="Question to answer using node-local KG evidence")
    node: str = Field(..., description="Base node name/title (or node id if known)")


class ask_kg_arguments(BaseModel):
    tool_name: str
    arguments: ask_kg_args


ask_kg_arguments.model_rebuild()
