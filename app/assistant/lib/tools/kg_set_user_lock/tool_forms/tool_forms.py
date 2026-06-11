from typing import Optional

from pydantic import BaseModel, Field


class kg_set_user_lock_args(BaseModel):
    node_id: str = Field(description="UUID of the node to lock or unlock.")
    locked: bool = Field(description="true to lock (axiom), false to unlock (provisional).")
    reason: str = Field(description="Why the lock state is changing. Goes into kg_revision_log.")
    dry_run: Optional[bool] = Field(default=False, description="Preview the change; do not commit.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_set_user_lock_arguments(BaseModel):
    tool_name: str
    arguments: kg_set_user_lock_args


kg_set_user_lock_arguments.model_rebuild()
