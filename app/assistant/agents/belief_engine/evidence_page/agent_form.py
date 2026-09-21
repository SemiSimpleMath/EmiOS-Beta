from pydantic import BaseModel

class Finding(BaseModel):
    fragment_id: str
    findings: str

class AgentForm(BaseModel):
    findings: list[Finding]
