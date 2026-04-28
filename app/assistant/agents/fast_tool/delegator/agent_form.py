from pydantic import BaseModel


class AgentForm(BaseModel):
    reason: str
    next_agent: str
