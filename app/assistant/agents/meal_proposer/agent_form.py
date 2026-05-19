"""Output schema for meal_proposer agent.

The agent emits a MealProposalSet covering the next 24-48 hours, plus
optionally a shopping run consolidating ingredients needed, plus an
optional fast-food advisory when the recent count crosses threshold.

v0 is propose-only: proposals become intention.meal / intention.shopping
pods. No calendar writes from the agent itself.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


MealWindow = Literal["breakfast", "lunch", "dinner", "snack"]
MealSource = Literal["home_cooked", "leftover", "delivery", "restaurant", "fast_food"]
Confidence = Literal["high", "medium", "low"]
Novelty = Literal["familiar", "variation", "novel"]


class ShoppingRun(BaseModel):
    """One consolidated shopping list across the proposal set."""
    model_config = ConfigDict(extra="forbid")
    suggested_date: str = Field(description="ISO date when to do the shopping run.")
    items: List[str] = Field(description="Deduped ingredient list across all proposed meals.")
    reasoning: str = Field(max_length=300, description="1-2 sentences on timing + scope.")


class MealProposal(BaseModel):
    """One proposed meal."""
    model_config = ConfigDict(extra="forbid")
    actors: List[str] = Field(
        description="Household members eating. Subset of family roster's 'home' set."
    )
    meal_window: MealWindow
    date: str = Field(description="ISO date.")
    proposed_start_local: str = Field(description="ISO datetime with tz offset.")
    dish: str = Field(
        max_length=200,
        description="Concise, specific. 'salmon + roasted broccoli + rice', NOT 'fish dinner'.",
    )
    source: MealSource
    primary_ingredients: List[str] = Field(
        description="Lowercase ingredient names without qualifiers ('salmon' not '2lb wild salmon')."
    )
    needs_shopping: List[str] = Field(
        description="Subset of primary_ingredients NOT in current inventory. Empty if all on hand."
    )
    estimated_calories_per_person: int = Field(ge=0, le=3000)
    estimated_total_cost_usd: Optional[float] = Field(default=None, ge=0)
    recipe_ref: Optional[str] = Field(
        default=None,
        description="Name of a recipe in recipes_house if applicable. Null if novel or external (restaurant).",
    )
    reasoning: str = Field(
        max_length=400,
        description=(
            "1-2 sentences tied to inputs. 'Inventory has X expiring tomorrow', "
            "'Katy is out so simpler meal', 'below protein goal today', etc."
        ),
    )
    confidence: Confidence
    novelty: Novelty = Field(
        description=(
            "familiar=dish in recipes_house. "
            "variation=familiar with a twist (different protein, new sauce). "
            "novel=something this family hasn't tried (new restaurant, new cuisine)."
        ),
    )
    novelty_rationale: Optional[str] = Field(
        default=None,
        max_length=300,
        description=(
            "Required when novelty != 'familiar'. Why try this novel thing now? "
            "What signal in the family graph suggests they'd like it?"
        ),
    )
    addresses_concern_ids: List[str] = Field(
        default_factory=list,
        description=(
            "Concern IDs from concerns_register that this proposal addresses. "
            "e.g., a meal proposing earlier-lighter dinner to address Jukka's "
            "fatigue concern lists that concern's ID here."
        ),
    )


class AgentForm(BaseModel):
    """Top-level meal_proposer output — agent runtime expects this exact name."""
    model_config = ConfigDict(extra="forbid")
    proposals: List[MealProposal] = Field(default_factory=list)
    skipped_meals: List[str] = Field(
        default_factory=list,
        description=(
            "Meal windows intentionally NOT proposed and why. "
            "e.g. 'today's breakfast: IF window per dietary_context'."
        ),
    )
    fast_food_advisory: Optional[str] = Field(
        default=None,
        max_length=400,
        description=(
            "1-2 sentences. Only emit when fast_food_count_7d >= 3. "
            "Otherwise omit (null)."
        ),
    )
    shopping_run: Optional[ShoppingRun] = Field(
        default=None,
        description="If any proposal has needs_shopping items, consolidate here.",
    )
    free_form_thinking: str = Field(
        max_length=1000,
        description=(
            "3-5 sentences on the shape of the proposal set. "
            "What's the week's eating like? What did you NOT propose and why? "
            "Concerns you noticed but couldn't fully address. Open questions."
        ),
    )
