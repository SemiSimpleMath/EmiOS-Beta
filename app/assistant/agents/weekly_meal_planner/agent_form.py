"""Output schema for weekly_meal_planner.

Strategic planning of the week's meal rhythm. Outputs:
- WeeklyMealPlan: 7-day grid where every dinner and every lunch carries
  a concrete dish, categorized by where the meal comes from (home cook,
  fast food, takeout, dine-out, leftover, novelty). Breakfast stays
  free-choice or skipped (IF).
- GroceryList: the week's whole Ralphs order (2026-11-14) — ingredients for
  the home-cook + novelty slots plus the standing staples needed this week,
  minus what is in the inventory; each item with its reason, and the staples
  left off with why. The owner reviews and approves it on /meals.

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


class GroceryItem(BaseModel):
    """One thing to order from Ralphs this week."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=120, description="The product, as you would search for it at Ralphs.")
    quantity: str = Field(max_length=60, description="How much, e.g. '2 lb', '1 dozen', '2 boxes'.")
    category: str = Field(
        max_length=40,
        description="Store section: Produce, Meat & Seafood, Dairy & Eggs, Bakery, Frozen, Pantry, Snacks, "
                    "Drinks, Household, or Other.",
    )
    reason: str = Field(
        max_length=200,
        description="Why it is on the list, in words: the meal(s) it is for, or the staple and why it is due.",
    )


class SkippedStaple(BaseModel):
    """A standing staple left off this week's order."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=120)
    reason: str = Field(max_length=200, description="Why not this week, in words.")


class GroceryList(BaseModel):
    """The week's whole Ralphs order, for the owner to review and approve on /meals."""
    model_config = ConfigDict(extra="forbid")
    items: List[GroceryItem]
    skipped_staples: List[SkippedStaple] = Field(
        description="Every standing staple you left off this week, each with why.",
    )
    note: str = Field(
        default="",
        max_length=600,
        description="Anything the owner should know when reviewing the list (1-3 sentences), or empty.",
    )


class AgentForm(BaseModel):
    """Top-level weekly_meal_planner output."""
    model_config = ConfigDict(extra="forbid")
    weekly_plan: WeeklyMealPlan
    grocery_list: GroceryList
    free_form_thinking: str = Field(
        max_length=1000,
        description=(
            "3-5 sentences on the week's shape, the trade-offs, what you left "
            "open and why, and any open questions worth raising."
        ),
    )
