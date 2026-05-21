"""Output schema for meal_context_distiller.

The distiller is a cheap LLM step that runs every meal-planning cycle.
Its job:
  - Pick the relevant beliefs (out of the full food-domain belief set).
  - Organize calendar / audience / concern signals UNDER the days they
    affect — same shape as daily_context_tracker: facts grouped per day
    so a reader can scan day-by-day.
  - Keep household-wide signals (concerns affecting the whole horizon,
    inventory pressures spanning multiple days) above the per-day grid.

It does NOT fetch new data. It does NOT decide meals. It REORGANIZES
existing seed into a form the meal_planner can read directly.

Persistence: this AgentForm is what gets written to the resource file
(resource_meal_context_distiller_output.json) and made available to the
downstream meal_planner as a system context item.
"""
from typing import List

from pydantic import BaseModel, ConfigDict, Field


class DayContext(BaseModel):
    """Meal-relevant signals for one day in the planning horizon."""
    model_config = ConfigDict(extra="forbid")

    date: str = Field(description="ISO date, e.g. '2026-05-25'.")
    day_of_week: str = Field(description="'Monday', 'Tuesday', ...")

    audience_notes: str = Field(
        default="",
        description=(
            "Who is eating which meals that day. Default household unless "
            "calendar / chat shows otherwise. Note deviations explicitly: "
            "'Peter at BAS — not at dinner', 'Marika arriving 3pm Sat — "
            "joining dinner', 'Katy traveling — solo Jukka meals'. "
            "Empty string when default audience applies for all windows."
        ),
    )

    events_or_constraints: str = Field(
        default="",
        description=(
            "Calendar events or situational constraints that affect meals "
            "this day. e.g. 'Annika has school choir 6-8 PM — late "
            "dinner', 'Wed Friday Night Meats Zoom — usual social slot', "
            "'Day trip to Palm Desert — eat out / quick lunch'. "
            "Empty string if nothing day-specific."
        ),
    )

    notes: str = Field(
        default="",
        description=(
            "Other day-specific notes worth surfacing to the meal_planner. "
            "Anniversaries, kid sleepovers, anything that shapes the day's "
            "meal decision but doesn't fit audience or event. "
            "Empty when nothing."
        ),
    )


class AgentForm(BaseModel):
    """Distilled meal-planning context for the upcoming horizon."""
    model_config = ConfigDict(extra="forbid")

    horizon_summary: str = Field(
        max_length=300,
        description=(
            "One- or two-line summary of the planning horizon's shape. "
            "e.g. 'Standard week — default household, salmon expires "
            "Mon, Jukka fatigue trend continuing; Marika visiting Sat.' "
            "First thing the meal_planner reads."
        ),
    )

    relevant_beliefs: List[str] = Field(
        default_factory=list,
        description=(
            "Food-domain beliefs from the BeliefStore that materially "
            "affect THIS horizon's meal decision. Each item is one full "
            "sentence. Pull from the food_beliefs seed — keep the most-"
            "actionable, drop the redundant. Typical count: 5-15. e.g.: "
            "'Jukka no longer doing IF — manage via calorie restriction.' "
            "'Red wine is a headache trigger for Jukka — hard no.' "
            "'Friday Night Meats is a Zoom social ritual, not a meat "
            "constraint.' "
            "Beliefs that ONLY apply on a specific day belong in that "
            "day's notes, not here."
        ),
    )

    horizon_wide_concerns: str = Field(
        default="",
        description=(
            "Concerns from the noticer that apply to the whole horizon "
            "(not to one specific day). e.g. 'Jukka fatigue pattern "
            "this week — favor lighter dinners, earlier serving.' "
            "Day-scoped concerns go in that day's notes. "
            "Empty if no horizon-wide concerns."
        ),
    )

    inventory_pressures: str = Field(
        default="",
        description=(
            "Use-soon items, shortages, and inventory facts the meal_"
            "planner should respect when picking anchors. e.g. 'Salmon "
            "— use within 1 day; chicken within 2.' Skip generic stocked "
            "items. Empty if no pressures."
        ),
    )

    days: List[DayContext] = Field(
        default_factory=list,
        description=(
            "Per-day breakdown for the planning horizon (typically 7 days "
            "for weekly, 2 days for daily). One entry per day, in order."
        ),
    )

    additional_notes: str = Field(
        default="",
        description=(
            "Catch-all for anything else the meal_planner should know "
            "that doesn't fit the structured buckets above. Use sparingly. "
            "Empty by default."
        ),
    )
