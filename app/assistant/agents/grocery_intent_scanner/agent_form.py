"""Output schema for grocery_intent_scanner.

The scanner reads recent user chat + active intention pods and detects
four kinds of inventory-mutating intents the user expressed.

This is an OBSERVER not an actor — it identifies intents; the runner
applies them via grocery_inventory.apply_*.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


IntentKind = Literal[
    "shopping_done",     # "I did groceries" / "back from costco" — applies a shopping_intention pod
    "meal_consumed",     # "I had the salmon" / "we ate the chicken" — applies a meal_intention pod
    "manual_acquire",    # "we got X" / "picked up Y" — outside any intention pod
    "manual_consume",    # "we ran out of X" / "I used the Y" — manual decrement
]


class GroceryIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: IntentKind = Field(description="Which mutation type this intent triggers.")

    chat_msg_id: str = Field(
        description=(
            "ID of the user message that carried this intent (from the "
            "recent_chat list you were given). Used so future scans can "
            "skip messages already processed."
        ),
    )

    chat_msg_text: str = Field(
        max_length=300,
        description="Verbatim user message that triggered detection. Audit trail.",
    )

    # For shopping_done / meal_consumed — the intention pod this applies to
    intention_pod_id: Optional[str] = Field(
        default=None,
        description=(
            "For shopping_done: the intention.shopping pod that was acted on. "
            "For meal_consumed: the intention.meal pod that was acted on. "
            "Null for manual_acquire / manual_consume."
        ),
    )

    # For manual_acquire / manual_consume — the items
    items: List[str] = Field(
        default_factory=list,
        description=(
            "For manual_acquire: items the user said they got. "
            "For manual_consume: items the user said they ran out of / used. "
            "Empty for shopping_done / meal_consumed (those are sourced from the pod)."
        ),
    )

    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "How confident you are this is the right interpretation. "
            "Low = ambiguous, runner should skip and log. "
            "Medium = probable but might affect wrong pod. "
            "High = explicit match."
        ),
    )

    reasoning: str = Field(
        max_length=300,
        description="Why you matched this intent to this pod / these items.",
    )


class AgentForm(BaseModel):
    """Top-level scanner output."""
    model_config = ConfigDict(extra="forbid")

    intents: List[GroceryIntent] = Field(
        default_factory=list,
        description="All detected intents from the scanned chat window. Empty if nothing matched.",
    )

    skipped_chat_msg_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Chat messages you considered but did NOT classify as grocery intents. "
            "Helps the runner avoid re-scanning them."
        ),
    )

    notes: str = Field(
        default="",
        max_length=500,
        description=(
            "Free-form: anything ambiguous, anything you wanted to flag, "
            "patterns worth knowing. Optional."
        ),
    )
