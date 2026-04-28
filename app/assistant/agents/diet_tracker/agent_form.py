from typing import List, Optional

from pydantic import BaseModel, Field


class DietItem(BaseModel):
    """A single thing the user ate or drank."""

    food: str = Field(
        description=(
            "Short name of the food or drink. Examples: 'yogurt', 'black coffee', "
            "'banana', 'water'. Keep it concrete — what was actually consumed."
        ),
    )
    quantity: Optional[str] = Field(
        default=None,
        description=(
            "Rough quantity in whatever form the user stated. Examples: '1 cup', "
            "'2 slices', 'a bowl', 'small glass'. Leave empty if not stated — "
            "don't invent a number."
        ),
    )
    eaten_at_local: str = Field(
        description=(
            "REQUIRED. Local date+time the user actually ate/drank this, in "
            "format 'YYYY-MM-DD HH:MM' (24h). This is the primary dedup key — "
            "two items with the same food and same eaten_at_local are the "
            "same entry. Use the message-level timestamp inside the pod body "
            "when available (e.g. '- [4:18 PM] Jukka: Time to have a yogurt' "
            "combined with the pod's date gives you 'YYYY-MM-DD 16:18'). "
            "Fall back to the pod's creation time if the body has no per-"
            "message timestamp. NEVER use a phrase like 'breakfast' — always "
            "a real date+time."
        ),
    )
    eaten_at_utc: str = Field(
        description=(
            "REQUIRED. Same instant as eaten_at_local but in ISO UTC format "
            "(e.g. '2026-04-19T16:18:00+00:00'). Used for precise dedup and "
            "time-window queries. Derive from eaten_at_local using the "
            "session timezone."
        ),
    )
    meal_kind: Optional[str] = Field(
        default=None,
        description=(
            "Coarse classification: 'meal' (breakfast/lunch/dinner), 'snack', "
            "'drink', 'coffee'. Leave empty if ambiguous."
        ),
    )
    source_pod_ids: List[str] = Field(
        default_factory=list,
        description=(
            "pod_ids of the food/drink pods that justify this entry. Every item "
            "must cite at least one pod_id — no uncited items."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        description=(
            "Optional short note — hedges, qualifiers, related context. Keep "
            "empty unless it carries real information."
        ),
    )


class AgentForm(BaseModel):
    """Merged diet log for today.

    Input: the current day's diet log (what we had before this tick) plus a
    rendered block of NEW food pods since the last run. Output: the merged
    log with any new items appended and overlaps de-duplicated. Do NOT drop
    existing items unless they were plainly wrong or superseded.
    """

    items: List[DietItem] = Field(
        default_factory=list,
        description=(
            "All food/drink items for today, in chronological order where "
            "timestamps allow. This is the FULL list — include existing items "
            "from current_diet_log plus any new ones extracted from the new "
            "pods. Deduplicate when a new pod simply re-mentions something "
            "already logged."
        ),
    )
    summary: str = Field(
        default="",
        description=(
            "One short sentence summarizing today's intake so far. Example: "
            "'Light breakfast (yogurt+banana), two coffees, hydration steady.' "
            "Empty if the day has no items yet."
        ),
    )
    open_questions: List[str] = Field(
        default_factory=list,
        description=(
            "Short list of ambiguous mentions worth following up on. Example: "
            "'Pod mentioned \"a snack\" with no detail — was it a granola bar "
            "or something else?' Keep empty if everything was unambiguous."
        ),
    )
    reasoning: str = Field(
        default="",
        description=(
            "Brief note on what was added/merged this tick and why. For "
            "debugging only; keep it to 1–2 sentences."
        ),
    )
