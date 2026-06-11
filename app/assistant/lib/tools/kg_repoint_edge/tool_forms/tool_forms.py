from typing import Optional

from pydantic import BaseModel, Field


class kg_repoint_edge_args(BaseModel):
    edge_id: str = Field(description="UUID of the edge to re-point.")
    reason: str = Field(description="Why this re-point is correct. Goes into kg_revision_log.")
    new_source_id: Optional[str] = Field(default=None, description="New source node UUID. Omit to keep the current source.")
    new_target_id: Optional[str] = Field(default=None, description="New target node UUID. Omit to keep the current target.")
    dry_run: Optional[bool] = Field(default=False, description="Preview the re-point; do not commit.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_repoint_edge_arguments(BaseModel):
    tool_name: str
    arguments: kg_repoint_edge_args


kg_repoint_edge_arguments.model_rebuild()
