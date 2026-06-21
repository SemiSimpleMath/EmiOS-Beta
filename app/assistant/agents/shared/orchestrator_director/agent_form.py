from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    """Structured output for `shared::orchestrator_director` — the CEO/director brain.

    It does NOT decompose (architect) or decide DONE (curator). It sets DIRECTION: at a
    phase boundary it either commits ONE binding directional decision into the charter, or
    passes. Scalar fields only (OpenAI/Gemini schema-safe)."""

    model_config = ConfigDict(extra="forbid")

    decision_needed: bool = Field(
        False,
        description="True ONLY when the consolidated results present a directional FORK that "
        "downstream work depends on (e.g. several researched options, someone must pick one) AND "
        "the standing charter has not already settled it. Most phase boundaries: false.",
    )
    decision: str = Field(
        "",
        description="The single committed directional decision, stated as a directive the whole team "
        "executes under (e.g. 'Build an AI-driven content-repurposing service for B2B SaaS'). "
        "Empty when decision_needed is false.",
    )
    rationale: str = Field(
        "",
        description="One or two sentences: why this option over the alternatives.",
    )
    notes: str = Field(
        ...,
        description="Short explanation of the call — or, when decision_needed is false, why no "
        "directional decision is needed yet.",
    )


AgentForm.model_rebuild()
