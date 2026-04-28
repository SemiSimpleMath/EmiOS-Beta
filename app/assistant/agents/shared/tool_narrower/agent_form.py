from pydantic import BaseModel


class AgentForm(BaseModel):
    reason: str
    likely_tools: list[str]
