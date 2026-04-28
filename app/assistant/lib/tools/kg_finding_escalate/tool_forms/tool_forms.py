from typing import Optional

from pydantic import BaseModel, Field


class kg_finding_escalate_args(BaseModel):
    finding_id: str = Field(description="kg_maintenance_finding.id to escalate.")
    summary: Optional[str] = Field(default=None, description="Short reviewer-facing summary.")
    suggested_action: Optional[str] = Field(default=None, description="Agent's recommended fix.")
    notes: Optional[str] = Field(default=None, description="Free-form notes.")
    reason: str = Field(description="Why escalate vs auto-execute.")


class kg_finding_escalate_arguments(BaseModel):
    tool_name: str
    arguments: kg_finding_escalate_args


kg_finding_escalate_arguments.model_rebuild()
