"""Output schema for kg_maintenance::identity_sentence_writer.

One definite description: the minimal sentence that uniquely picks out
this node's referent among everything in the user's knowledge graph —
or an honest verdict that no coherent referent can be described."""
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definable: bool = Field(
        description=(
            "True when the inputs single out a coherent real-world referent. "
            "False when they cannot — a node whose only anchors are abstract "
            "state plumbing ('the condition targeted by Trigger Logic "
            "Preference') has no describable referent; say so instead of "
            "padding."
        )
    )
    identity_sentence: Optional[str] = Field(
        default=None,
        max_length=220,
        description=(
            "Required when definable=true. The minimal definite description "
            "that uniquely identifies this referent: lead with the most "
            "discriminating anchors (possessor, location, role-holder), "
            "include the era when dated. Examples: \"Jouko and Susie's house "
            "in Marysville.\" / \"The school the user's daughter attends "
            "(since fall 2024).\" Never restate incidental facts."
        ),
    )
    basis: str = Field(
        max_length=200,
        description=(
            "Which input facts the sentence leans on — or when "
            "definable=false, what is missing."
        ),
    )
