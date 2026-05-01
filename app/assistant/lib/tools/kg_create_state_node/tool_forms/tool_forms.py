from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class kg_create_state_node_args(BaseModel):
    owner_node_id: str = Field(description="Existing entity that 'owns' this state (the source of the new edge).")
    predicate: str = Field(description="relationship_type for the new edge, e.g. 'has_residence', 'has_state'.")
    label: str = Field(description="Label for the new State / Event node.")
    node_type: Optional[str] = Field(
        default="State",
        description="'State' (default) or 'Event'. Other types are not allowed for this tool.",
    )
    category: Optional[str] = Field(default=None, description="e.g. 'residence', 'job', 'membership'.")
    description: Optional[str] = Field(default=None, description="Free-form description of the era / event.")
    original_sentence: Optional[str] = Field(default=None, description="The user-provided sentence this is derived from, if any.")
    start_date: Optional[str] = Field(default=None, description="ISO date or datetime, e.g. '1990-01-01'.")
    end_date: Optional[str] = Field(default=None, description="ISO date or datetime; omit for an open-ended era.")
    start_date_confidence: Optional[str] = Field(default=None, description="e.g. 'operator_provided', 'estimated'.")
    end_date_confidence: Optional[str] = Field(default=None, description="As above for end_date.")
    sentence: Optional[str] = Field(default=None, description="Sentence to attach to the owner→state edge.")
    attributes: Optional[Dict[str, Any]] = Field(default=None, description="Extra structured attrs (JSON dict).")
    reason: str = Field(description="Why this state should exist. Goes into kg_revision_log.")
    dry_run: Optional[bool] = Field(default=False, description="Preview only; no commit.")
    finding_id: Optional[str] = Field(default=None, description="Source kg_maintenance_finding.id, if any.")


class kg_create_state_node_arguments(BaseModel):
    tool_name: str
    arguments: kg_create_state_node_args


kg_create_state_node_arguments.model_rebuild()
