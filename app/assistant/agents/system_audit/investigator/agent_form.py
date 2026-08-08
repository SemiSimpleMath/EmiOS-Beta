"""Structured output for the system investigator."""
from typing import List
from pydantic import BaseModel, Field


class RepairOption(BaseModel):
    level: str = Field(description="prompt | config | code")
    description: str = Field(description="The concrete repair, one or two sentences.")


class AgentForm(BaseModel):
    summary: str = Field(description="One sentence: what actually went wrong.")
    causal_chain: str = Field(description="The step-by-step chain from trigger to failure, citing evidence ids/lines from the dossier.")
    implicated_subsystem: str = Field(description="Short stable slug for the responsible subsystem (e.g. dispatch, evaluator, scope, tickets, kg, scheduler, prompts).")
    repair_options: List[RepairOption] = Field(default_factory=list)
    confidence: float = Field(description="0..1 confidence in the causal chain.")
    needs_claude: bool = Field(description="True when the repair requires reading or changing code (hand to a Claude Code session).")
