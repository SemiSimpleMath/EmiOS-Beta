from typing import Optional

from pydantic import BaseModel, Field


class kg_rename_label_args(BaseModel):
    node_id: str = Field(description="Target node id.")
    new_label: str = Field(description="New canonical label.")
    reason: str = Field(description="Why the rename is correct.")
    dry_run: Optional[bool] = Field(default=False, description="Preview only; default false.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_rename_label_arguments(BaseModel):
    tool_name: str
    arguments: kg_rename_label_args


kg_rename_label_arguments.model_rebuild()
