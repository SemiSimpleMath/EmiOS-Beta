from pydantic import BaseModel


class AgentForm(BaseModel):
    reason: str
    selected_tool: str