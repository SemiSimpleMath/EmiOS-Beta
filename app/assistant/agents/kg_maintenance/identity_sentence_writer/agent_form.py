"""Output schema for kg_maintenance::identity_sentence_writer.

One definite description: the minimal sentence that uniquely picks out
this node's referent among everything in the user's knowledge graph."""
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_sentence: str = Field(
        max_length=220,
        description=(
            "The minimal definite description that uniquely identifies this "
            "referent: lead with the most discriminating anchors (possessor, "
            "location, role-holder), include the era when dated. Examples: "
            "\"Jouko and Susie's house in Marysville.\" / \"The school the "
            "user's daughter attends (since fall 2024).\" Never restate "
            "incidental facts (thermostat counts, one-off events)."
        ),
    )
    basis: str = Field(
        max_length=200,
        description="Which input facts the sentence leans on (for the log).",
    )
