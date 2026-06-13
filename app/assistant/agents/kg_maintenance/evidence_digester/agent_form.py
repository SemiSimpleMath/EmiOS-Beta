"""Output schema for kg_maintenance::evidence_digester.

One consolidated digest: the old evidence tail folded into a compact
dated chronicle that preserves every materially distinct fact."""
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digest: str = Field(
        max_length=2000,
        description=(
            "The consolidated chronicle of the input evidence rows: "
            "chronological, dated, third person. Every materially distinct "
            "fact survives with its date ('Started piano lessons (2024-03)'); "
            "repeated observations of the same fact collapse to one line, "
            "optionally with the observation span ('mentioned repeatedly "
            "2024-03..2025-01'). Never invent facts not present in the input."
        ),
    )
