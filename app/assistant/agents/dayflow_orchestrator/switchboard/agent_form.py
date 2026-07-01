from pydantic import BaseModel


class AgentForm(BaseModel):
    reason: str
    delegate_to: str        # the NAME of the tool to call for this node (create_dayflow_ticket | run_work_node | ...)
