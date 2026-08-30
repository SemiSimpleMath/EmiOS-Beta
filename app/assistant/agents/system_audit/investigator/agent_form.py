"""Structured output for the system investigator."""
from typing import List, Optional
from pydantic import BaseModel, Field


class RepairOption(BaseModel):
    level: str = Field(description="prompt | config | code")
    description: str = Field(description="The concrete repair, one or two sentences.")


class AgentForm(BaseModel):
    summary: str = Field(description="One sentence: what actually went wrong.")
    causal_chain: str = Field(description="The step-by-step chain from trigger to failure, citing evidence ids/lines from the dossier.")
    implicated_subsystem: Optional[str] = Field(
        default=None,
        description=(
            "Short stable slug for the responsible subsystem (e.g. dispatch, evaluator, "
            "scope, tickets, kg, scheduler, prompts) — ONLY when the evidence in the "
            "dossier actually identifies the layer. Leave it null when the evidence shows "
            "WHAT went wrong but not WHICH component is responsible. A wrong slug is worse "
            "than none: it sends the repair to the wrong layer, and a case blaming the "
            "component that merely acted on bad input (the scheduler firing exactly the "
            "time it was handed) buries the component that produced it."
        ),
    )
    repair_options: List[RepairOption] = Field(default_factory=list)
    confidence: float = Field(description="0..1 confidence in the causal chain.")
    needs_claude: bool = Field(description="True when the repair requires reading or changing code (hand to a Claude Code session).")
