from pydantic import BaseModel


class AgentForm(BaseModel):
    reason: str
    delegate_to: str
    ticket_kind: str = ""   # when delegate_to=create_dayflow_ticket: "notify" (one-way) | "decision" (needs a reply)
    task: str
    task_information: str
