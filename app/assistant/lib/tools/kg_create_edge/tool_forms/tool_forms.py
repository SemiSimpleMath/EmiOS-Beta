from typing import Optional

from pydantic import BaseModel, Field


class kg_create_edge_args(BaseModel):
    source_id: str = Field(description="UUID of the source node.")
    target_id: str = Field(description="UUID of the target node.")
    relationship_type: str = Field(description="Predicate, e.g. 'works_for', 'attended'.")
    reason: str = Field(description="Why this edge is correct. Goes into kg_revision_log.")
    sentence: Optional[str] = Field(default=None, description="Natural-language statement of the relationship.")
    dry_run: Optional[bool] = Field(default=False, description="Preview the edge; do not commit.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_create_edge_arguments(BaseModel):
    tool_name: str
    arguments: kg_create_edge_args


kg_create_edge_arguments.model_rebuild()
