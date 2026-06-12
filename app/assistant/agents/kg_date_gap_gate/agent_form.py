"""Output schema for kg_maintenance::date_gap_gate.

Gate + craft: is this undated state worth asking the user about, and if
so, what's the natural question?"""
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worthy: bool = Field(
        description=(
            "True when knowing WHEN this began/ended genuinely enriches the "
            "user's life record: education, jobs, homes, relationships, "
            "marriages, births, big purchases (house, car), memberships that "
            "matter. False for everyday objects (furniture, gadgets, "
            "clothes), minor acquisitions, and anything whose dates nobody "
            "would ever look up."
        )
    )
    skip_reason: Optional[str] = Field(
        default=None, max_length=200,
        description="Required when worthy=false.",
    )
    question_text: Optional[str] = Field(
        default=None, max_length=300,
        description=(
            "Required when worthy=true. A natural, SPECIFIC question using the "
            "real entities — 'When did you graduate from UC Berkeley?', 'Roughly "
            "when did you and Sam become friends?' — never the raw state label "
            "('when did the Ownership state begin')."
        ),
    )
