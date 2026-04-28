from pydantic import BaseModel


class AgentForm(BaseModel):
    response: str