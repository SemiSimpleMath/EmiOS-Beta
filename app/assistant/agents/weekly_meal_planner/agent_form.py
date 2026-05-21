"""Output schema for weekly_meal_planner.

Strategic planning of the week's meal rhythm. Outputs:
- WeeklyMealPlan: 7-day grid of meal-window SLOT TYPES (anchor / leftover /
  flex / novel_try / skip) — not specific dishes; the daily proposer
  decides what dish goes in each slot.
- WeeklyShoppingList: ingredients across the week minus inventory minus
  Ralphs staples. The persist step writes/replaces a Google Doc.
- Anchor meals: the 1-3 specific dishes the week is built around (e.g.,
  "Friday Night Meats", "Sunday roast", "Tuesday salmon").

The daily proposer reads the most recent plan.weekly_meals pod as context
and turns the day's slots into actual dishes.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


MealWindow = Literal["breakfast", "lunch", "dinner", "snack"]
SlotType = Literal[
    "anchor",       # The week's planned dish for this slot (named in anchor_meals)
    "leftover",     # Eat leftovers from a previous anchor; no new cooking
    "flex",         # Day-of decision; daily proposer picks
    "novel_try",    # Intentional novelty attempt; daily proposer picks
    "skip",         # Intentionally skipped (e.g., IF window for breakfast)
]
WeeklyListAction = Literal["none", "create", "replace"]


class SlotPlan(BaseModel):
    """One meal slot in the week (e.g., Wednesday dinner)."""
    model_config = ConfigDict(extra="forbid")
    date: str = Field(description="ISO date.")
    day_of_week: str = Field(description="'Monday', 'Tuesday', etc.")
    meal_window: MealWindow
    slot_type: SlotType
    anchor_dish: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Required when slot_type='anchor'. The specific dish name. "
            "Should match one of the items in `anchor_meals`. "
            "Null for other slot types — daily proposer fills those."
        ),
    )
    leftover_from_date: Optional[str] = Field(
        default=None,
        description=(
            "Required when slot_type='leftover'. ISO date of the anchor "
            "meal whose leftovers fill this slot."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Optional context for the daily proposer when filling flex/novel_try slots.",
    )


class WeeklyMealPlan(BaseModel):
    """The 7-day skeleton — meal windows × slot types."""
    model_config = ConfigDict(extra="forbid")
    week_start_date: str = Field(description="ISO date for Monday of this week.")
    week_theme: str = Field(
        max_length=300,
        description=(
            "1-2 sentences describing the week's overall shape. e.g., "
            "'Lean week — Jukka has been tired; minimize heavy evening "
            "meals and lean on familiar anchors. Katy traveling Thursday "
            "so kid-friendly dinners those nights.'"
        ),
    )
    anchor_meals: List[str] = Field(
        description=(
            "1-3 specific dishes the week is built around. These appear in "
            "SlotPlan entries with slot_type='anchor'. e.g., "
            "['Salmon + roasted broccoli', 'Roast chicken', "
            "'Friday Night Meats — ribeye']."
        ),
    )
    slots: List[SlotPlan] = Field(
        description=(
            "All meal windows in the week. Should cover 7 days × (typically) "
            "breakfast + lunch + dinner = up to 21 slots. Skip windows that "
            "are reliably skipped (e.g., Jukka's breakfast during IF) by "
            "setting slot_type='skip'."
        ),
    )


class WeeklyShoppingList(BaseModel):
    """The agent's per-week shopping list — separate from Jukka's standing
    Ralphs list. Persist step calls create_google_doc or edit_google_doc."""
    model_config = ConfigDict(extra="forbid")
    action: WeeklyListAction = Field(
        description=(
            "'create' if state has no doc_id; 'replace' if it does; "
            "'none' to skip the doc write this run."
        ),
    )
    body_markdown: str = Field(
        default="",
        max_length=8000,
        description=(
            "Full markdown body. Required when action != 'none'. Organize "
            "by category (## Produce / ## Meat & Seafood / ## Dairy / "
            "## Pantry / ## Other). EXCLUDE items already on the Ralphs "
            "standing list (staples coming anyway) and items currently in "
            "inventory."
        ),
    )
    week_start_date: Optional[str] = Field(
        default=None,
        description="ISO date for the Monday this list covers. Required when action != 'none'.",
    )


class AgentForm(BaseModel):
    """Top-level weekly_meal_planner output."""
    model_config = ConfigDict(extra="forbid")
    weekly_plan: WeeklyMealPlan
    weekly_shopping_list: WeeklyShoppingList
    free_form_thinking: str = Field(
        max_length=1000,
        description=(
            "3-5 sentences on the week's shape, the trade-offs, what you left "
            "open and why, and any open questions worth raising."
        ),
    )
