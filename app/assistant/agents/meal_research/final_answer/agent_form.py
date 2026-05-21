"""Output schema for meal_research::final_answer.

This is meal_research_manager's output CONTRACT. The calling planner
(typically weekly_meal_planner) reads `finding` back through the
tool_result of its meal_research_manager call.

`finding` is intentionally a string, not a richer structure — the
calling planner is an LLM that reads prose; structuring it further
adds friction without gain at this point.
"""
from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding: str = Field(
        description=(
            "Concise research finding answering the caller's question. "
            "1-5 sentences typical. If the KG/pods don't have the info, "
            "say so honestly: 'No record of Marika's dietary profile in "
            "stored memory.' If you found something useful, state it "
            "directly: 'Marika is Katy's sister, KG-tagged with "
            "peanut allergy. Last shared meal pod 2026-03-15 was a "
            "tomato-free pasta night that went well.'"
        ),
    )

    confidence: str = Field(
        default="medium",
        description=(
            "How confident the finding is: 'high' (multiple consistent "
            "sources or explicit user-stated fact), 'medium' (one solid "
            "source), 'low' (inferred or partial). The caller can weigh "
            "the finding accordingly."
        ),
    )
