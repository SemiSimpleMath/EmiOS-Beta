from typing import Optional

from pydantic import BaseModel, Field


class kg_close_state_args(BaseModel):
    node_id: str = Field(description="Existing State or Event node id to close.")
    end_date: str = Field(description="ISO date or datetime, e.g. '1992-06-01'.")
    end_date_confidence: Optional[str] = Field(
        default="operator_close",
        description="Default 'operator_close'. Other values: 'estimated', 'agent_close', etc.",
    )
    end_date_prose: Optional[str] = Field(
        default=None,
        description="Free-form prose for the end-date (e.g. 'June 1992')."
    )
    force: Optional[bool] = Field(
        default=False,
        description="Set true to overwrite an existing end_date. The prior value is captured in kg_revision_log.",
    )
    reason: str = Field(description="Why the era ended. Goes into kg_revision_log.")
    dry_run: Optional[bool] = Field(default=False, description="Preview only; no commit.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_close_state_arguments(BaseModel):
    tool_name: str
    arguments: kg_close_state_args


kg_close_state_arguments.model_rebuild()
