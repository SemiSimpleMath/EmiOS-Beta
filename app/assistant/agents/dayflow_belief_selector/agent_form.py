from pydantic import BaseModel


class AgentForm(BaseModel):
    belief_ids: list[str]
    reasoning: str
