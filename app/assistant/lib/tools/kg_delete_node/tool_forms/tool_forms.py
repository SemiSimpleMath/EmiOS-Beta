from typing import Optional

from pydantic import BaseModel, Field


class kg_delete_node_args(BaseModel):
    node_id: str = Field(description="UUID of the node to delete.")
    reason: str = Field(description="Why this deletion is correct. Goes into kg_revision_log.")
    dry_run: Optional[bool] = Field(default=False, description="Preview what would be deleted; do not commit.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_delete_node_arguments(BaseModel):
    tool_name: str
    arguments: kg_delete_node_args


kg_delete_node_arguments.model_rebuild()
