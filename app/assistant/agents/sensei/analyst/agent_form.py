from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class StepAnalysis(BaseModel):
    step_number: int
    action: str = ""
    tool_name: str = ""
    tool_args_summary: str = Field(default="", description="Key arguments: URL, coordinates, text typed, ref clicked")
    classification: Literal["invariant", "parameter", "fragile", "skip"] = "parameter"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    selectors: List[str] = Field(default_factory=list, description="CSS selectors or refs that were used")
    hardcode_url: Optional[str] = Field(default=None, description="If this step always navigates to the same URL")
    hardcode_input: Optional[str] = Field(default=None, description="If this step always types the same text")
    expected_after: str = Field(
        default="",
        description="What the page state should look like after this step succeeds. "
                    "Start vague ('menu visible'), refine with specifics as confidence grows "
                    "('URL contains /store/pizza-hut, search bar in header, menu categories visible').",
    )
    reasoning: str = ""


class KnownBlocker(BaseModel):
    description: str = Field(description="What the blocker looks like, e.g. 'Store is currently closed modal'")
    detection_hint: str = Field(
        default="",
        description="How to detect it from a snapshot, e.g. 'dialog with text containing currently closed'",
    )
    recommended_action: str = Field(
        default="",
        description="What to do: 'dismiss and continue', 'report and stop', 'click Schedule Order'",
    )
    first_seen_step: int = 0


class DiscoveredParameter(BaseModel):
    name: str = Field(description="Parameter name, e.g. 'restaurant_name', 'pizza_size'")
    description: str = ""
    example_values: List[str] = Field(default_factory=list)
    used_in_steps: List[int] = Field(default_factory=list)


class AgentForm(BaseModel):
    run_success: bool = False
    overall_assessment: str = Field(description="1-3 sentence summary of the run")
    step_analyses: List[StepAnalysis] = Field(default_factory=list)
    known_blockers: List[KnownBlocker] = Field(
        default_factory=list,
        description="Unexpected modals, popups, or states encountered during the run",
    )
    discovered_parameters: List[DiscoveredParameter] = Field(default_factory=list)
    failure_patterns: List[str] = Field(default_factory=list, description="Steps or patterns that failed/retried")
    optimization_suggestions: List[str] = Field(default_factory=list, description="Concrete suggestions for the next run")
    estimated_deterministic_pct: float = Field(
        default=0.0,
        description="Percentage of steps that could be fully deterministic (0-100)",
    )
