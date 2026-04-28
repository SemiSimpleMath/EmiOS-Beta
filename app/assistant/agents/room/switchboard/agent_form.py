from pydantic import BaseModel

class AgentForm(BaseModel):
    reason: str
    delegate_to: str
    task: str
    task_information: str
