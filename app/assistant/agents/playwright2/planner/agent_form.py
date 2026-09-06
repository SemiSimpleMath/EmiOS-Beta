from pydantic import BaseModel


class AgentForm(BaseModel):
    note: str
    what_i_am_thinking: str
    checklist: list[str]
    plan: str
    action: str
    action_input: str
