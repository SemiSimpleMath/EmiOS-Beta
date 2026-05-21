"""Output schema for weekly_meal::context_planner.

Standard Planner shape: reasoning trace + checklist + action.

The planner's job is to decide whether the seed context is sufficient
for the weekly_meal_planner downstream, and if not, fetch the missing
piece via tool calls. When the seed (or the seed + accumulated tool
results) is enough, action='return_control' hands off to the actual
weekly meal planner.

Cold-start note: for a week with no precedent (no past plan.weekly_meals
pod, no recent feedback.comment pods on prior meals), the planner can
usually return control on the first cycle without using tools.
"""
from typing import List

from pydantic import BaseModel


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[str]
    plan: str
    action: str
    action_input: str
