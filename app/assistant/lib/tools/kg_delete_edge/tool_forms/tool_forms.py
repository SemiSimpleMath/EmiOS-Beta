from typing import Optional

from pydantic import BaseModel, Field


class kg_delete_edge_args(BaseModel):
    edge_id: str = Field(description="UUID of the edge to delete.")
    reason: str = Field(description="Why this deletion is correct. Goes into kg_revision_log.")
    dry_run: Optional[bool] = Field(default=False, description="Preview the deletion; do not commit.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_delete_edge_arguments(BaseModel):
    tool_name: str
    arguments: kg_delete_edge_args


kg_delete_edge_arguments.model_rebuild()
