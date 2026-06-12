from typing import List, Optional

from pydantic import BaseModel, Field


class kg_split_succession_args(BaseModel):
    node_id: str = Field(description="UUID of the role-reference Entity to split.")
    split_date: str = Field(description="ISO date where the referent changed (old era's end, successor's start).")
    reason: str = Field(description="Why this split is correct. Goes into kg_revision_log.")
    date_confidence: Optional[str] = Field(default="estimated", description="user_set | actual | estimated | inferred.")
    repoint_edge_ids: Optional[List[str]] = Field(default=None, description="Edges on the old node that belong to the NEW era; repointed onto the successor.")
    dry_run: Optional[bool] = Field(default=False, description="Preview the split; do not commit.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_split_succession_arguments(BaseModel):
    tool_name: str
    arguments: kg_split_succession_args


kg_split_succession_arguments.model_rebuild()
