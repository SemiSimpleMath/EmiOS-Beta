from pydantic import BaseModel


class AgentForm(BaseModel):
    report: str