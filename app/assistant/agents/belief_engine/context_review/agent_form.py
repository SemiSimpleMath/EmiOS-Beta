from typing import Literal
from pydantic import BaseModel, Field


class Revision(BaseModel):
    belief_id: str
    statement: str
    confidence: Literal["high", "medium", "low"]
    status: Literal["active", "contested", "deprecated"]
    conditions: str | None
    evidence_refs: list[str]
    reasoning: str
    applicability: str
    uncertainty: str
    related_belief_ids: list[str]


class Finding(BaseModel):
    belief_ids: list[str]
    explanation: str
    evidence_refs: list[str]


class AgentForm(BaseModel):
    phase: Literal["investigate", "finish"]
    search_queries: list[str] = Field(description="Natural-language requests for additional potentially relevant beliefs.")
    inspect_belief_ids: list[str]
    source_dates: list[str] = Field(description="YYYY-MM-DD source days to inspect in original master-room conversations, including assistant context.")
    reasoning: str
    revisions: list[Revision]
    preserved: list[Finding]
    unresolved: list[Finding]
    consumer_guidance: str
