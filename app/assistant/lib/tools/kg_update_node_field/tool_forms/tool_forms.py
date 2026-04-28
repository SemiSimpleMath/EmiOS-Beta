from typing import Any, Optional

from pydantic import BaseModel, Field


class kg_update_node_field_args(BaseModel):
    node_id: str = Field(description="Target node id.")
    field: str = Field(description="Field name (see contract for allowlist).")
    value: Any = Field(description="New value. For list fields, may be a string or list.")
    list_op: Optional[str] = Field(default=None, description="For aliases / hash_tags: add | remove | set.")
    reason: str = Field(description="Why the edit is correct.")
    dry_run: Optional[bool] = Field(default=False, description="Preview only; default false.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_update_node_field_arguments(BaseModel):
    tool_name: str
    arguments: kg_update_node_field_args


kg_update_node_field_arguments.model_rebuild()
