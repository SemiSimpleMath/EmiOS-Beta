from typing import List, Literal

from pydantic import BaseModel, Field


class PlannedToolCall(BaseModel):
    tool: str = Field(description="Tool name from the catalog.")
    args_json: str = Field(
        default="{}",
        description='Arguments as a JSON object string. Use concrete values when known.',
    )
    purpose: str = Field(
        default="",
        description="One-line description of what this tool call accomplishes.",
    )


class AgentForm(BaseModel):
    recommendation: Literal["deterministic", "manager"] = Field(
        description="'deterministic' for fixed tool sequence, 'manager' for open-ended LLM work.",
    )
    manager_name: str = Field(
        default="",
        description="Manager name when recommendation is 'manager'. Empty for deterministic.",
    )
    tools: List[PlannedToolCall] = Field(
        default_factory=list,
        description="Ordered tool calls when recommendation is 'deterministic'. Empty for manager.",
    )
    reasoning: str = Field(
        default="",
        description="Brief explanation of why this approach was chosen.",
    )
    produces_description: str = Field(
        default="",
        description="One-line description of what this step produces.",
    )
