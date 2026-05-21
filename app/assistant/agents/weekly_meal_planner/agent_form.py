"""Output schema for weekly_meal_planner.

Strategic planning of the week's meal rhythm. Outputs:
- WeeklyMealPlan: 7-day grid where every dinner and every lunch carries
  a concrete dish, categorized by where the meal comes from (home cook,
  fast food, takeout, dine-out, leftover, novelty). Breakfast stays
  free-choice or skipped (IF).
- WeeklyShoppingList: ingredients for the home-cook + novelty slots
  across the week, minus inventory and minus the Ralphs standing list.

The planner draws dishes/venues from the household_food_registry
(`resource_household_food_registry`). Most slots reuse known items
from the registry; at most one slot per week may be a `novelty` —
a new home dish or a new restaurant to try.

Lunches default to leftover from the previous night's dinner. Sat/Sun
lunches typically need a fresh planned dish if Friday/Saturday dinner
didn't produce usable leftovers.

There is NO downstream "fill the rest in day-of" step. Every dinner
and every lunch is decided here so the user can shop accordingly.
"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


MealWindow = Literal["breakfast", "lunch", "dinner", "snack"]
SlotType = Literal[
    "home_cook",   # A dish cooked at home — picked from the registry's home-cook section
    "fast_food",   # Fast-food order — venue named in `dish` (e.g., "In-N-Out")
    "takeout",     # Restaurant pickup/delivery — venue named in `dish`
    "dine_out",    # Sit-down restaurant — venue named in `dish`
    "leftover",    # Eat leftovers from a previous slot this week
    "novelty",     # A new dish or restaurant being tried — capped at 1/week
    "flex",        # BREAKFAST ONLY — household eats cereal/fruit/whatever ad hoc
    "skip",        # BREAKFAST ONLY — intentionally skipped (IF window)
]
WeeklyListAction = Literal["none", "create", "replace"]


class SlotPlan(BaseModel):
    """One meal slot in the week (e.g., Wednesday dinner)."""
    model_config = ConfigDict(extra="forbid")
    date: str = Field(description="ISO date.")
    day_of_week: str = Field(description="'Monday', 'Tuesday', etc.")
    meal_window: MealWindow
    slot_type: SlotType
    dish: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "The specific dish (for home_cook / novelty / leftover) OR the "
            "venue (for fast_food / takeout / dine_out). "
            "REQUIRED for everything except flex / skip. "
            "For home_cook, should match a dish in the registry's home-cook "
            "section. For leftover, repeat the source dish "
            "(e.g., 'leftovers from Lemon salmon'). For fast_food/takeout/"
            "dine_out, name the venue (e.g., 'In-N-Out' or 'Chipotle'); "
            "specific menu items can go in `notes` if useful. "
            "Null only when slot_type is 'flex' or 'skip' — both of which "
            "are BREAKFAST ONLY."
        ),
    )
    leftover_from_date: Optional[str] = Field(
        default=None,
        description=(
            "Required when slot_type='leftover'. ISO date of the slot "
            "whose leftovers fill this slot (usually previous day's dinner)."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Optional 1-line context: why this dish, prep timing, "
            "kid-friendly tweaks, etc."
        ),
    )


class WeeklyMealPlan(BaseModel):
    """The 7-day skeleton — every meal window × slot types."""
    model_config = ConfigDict(extra="forbid")
    week_start_date: str = Field(description="ISO date for Monday of this week.")
    week_theme: str = Field(
        max_length=400,
        description=(
            "2-3 sentences describing the week's overall shape: notable "
            "dishes, the fast-food/takeout count (and why), any health or "
            "fatigue considerations driving choices, and any novelty for "
            "the week. e.g., 'Lighter week — Jukka has been tired, so "
            "weeknight cooks are simple. One takeout slot Friday because "
            "the past 3 weeks were heavy on fast food and we want to "
            "ease back. Trying a new dish (chicken pad thai) Saturday.'"
        ),
    )
    slots: List[SlotPlan] = Field(
        description=(
            "All meal windows in the week. Covers 7 days × "
            "breakfast + lunch + dinner = 21 slots."
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
