from typing import Optional

from pydantic import BaseModel, Field


class kg_finding_resolve_args(BaseModel):
    finding_id: str = Field(description="kg_maintenance_finding.id to resolve.")
    action: Optional[str] = Field(default=None, description="merge_nodes | rename_label | update_node_field | delete_edge | no_action | acknowledged.")
    notes: Optional[str] = Field(default=None, description="Free-form notes (revision_log_id of the mutation, etc.).")
    reason: str = Field(description="Why the finding is now resolved.")


class kg_finding_resolve_arguments(BaseModel):
    tool_name: str
    arguments: kg_finding_resolve_args


kg_finding_resolve_arguments.model_rebuild()
