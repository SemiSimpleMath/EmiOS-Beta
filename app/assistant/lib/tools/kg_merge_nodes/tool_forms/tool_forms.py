from typing import Optional

from pydantic import BaseModel, Field


class kg_merge_nodes_args(BaseModel):
    keep_id: str = Field(description="Surviving node id (gains the alias + edges).")
    fold_id: str = Field(description="Node to fold and delete.")
    reason: str = Field(description="Why this merge is correct. Goes into kg_revision_log.")
    dry_run: Optional[bool] = Field(default=False, description="Preview the diff; do not commit.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_merge_nodes_arguments(BaseModel):
    tool_name: str
    arguments: kg_merge_nodes_args


kg_merge_nodes_arguments.model_rebuild()
