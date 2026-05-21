"""Output schema for daily_meal_proposer.

Tactical — turns the week's plan slots (from the latest plan.weekly_meals
pod, produced by weekly_meal_planner) into concrete dishes for the next
24-48 hours. Adapts to what actually happened today (inventory drift,
new concerns, family schedule changes).

Does NOT mutate the agent's weekly shopping doc — that's the weekly
planner's job. Daily emits per-meal intention.meal pods + optionally a
small ShoppingRun for any urgent items needed before the next weekly run.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


MealWindow = Literal["breakfast", "lunch", "dinner", "snack"]
MealSource = Literal["home_cooked", "leftover", "delivery", "restaurant", "fast_food"]
Confidence = Literal["high", "medium", "low"]
Novelty = Literal["familiar", "variation", "novel"]


class ShoppingRun(BaseModel):
    """Small ad-hoc shopping for items needed before the next weekly run."""
    model_config = ConfigDict(extra="forbid")
    suggested_date: str = Field(description="ISO date when to do this small run.")
    items: List[str]
    reasoning: str = Field(max_length=300)


class MealProposal(BaseModel):
    """One proposed meal for the next 24-48h."""
    model_config = ConfigDict(extra="forbid")
    actors: List[str]
    meal_window: MealWindow
    date: str
    proposed_start_local: str
    dish: str = Field(max_length=200)
    source: MealSource
    primary_ingredients: List[str]
    needs_shopping: List[str]
    estimated_calories_per_person: int = Field(ge=0, le=3000)
    estimated_total_cost_usd: Optional[float] = Field(default=None, ge=0)
    recipe_ref: Optional[str] = None
    reasoning: str = Field(max_length=400)
    confidence: Confidence
    novelty: Novelty
    novelty_rationale: Optional[str] = Field(default=None, max_length=300)
    fills_weekly_plan_slot: Optional[str] = Field(
        default=None,
        max_length=120,
        description=(
            "If this meal fills a slot from the weekly_plan, set this to "
            "'<ISO date>/<meal_window>/<slot_type>' so the audit trail "
            "links plan → concrete dish. Null if no weekly plan exists or "
            "this meal is off-plan."
        ),
    )


class AgentForm(BaseModel):
    """Top-level daily_meal_proposer output."""
    model_config = ConfigDict(extra="forbid")
    proposals: List[MealProposal] = Field(default_factory=list)
    skipped_meals: List[str] = Field(default_factory=list)
    fast_food_advisory: Optional[str] = Field(default=None, max_length=400)
    shopping_run: Optional[ShoppingRun] = Field(
        default=None,
        description="Small ad-hoc shopping; the weekly run is the main list. Use sparingly.",
    )
    free_form_thinking: str = Field(max_length=1000)
