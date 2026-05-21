"""Output schema for weekly_meal_planning::planner.

This planner sits in front of weekly_meal_planner inside
weekly_meal_planning_manager. Its job is NOT to plan meals — its job
is to decide whether the existing seed/distilled context is enough,
or whether a research call to meal_research_manager is needed first.

On most cycles it will return_control immediately. Tool calls are
the exception, not the default.
"""
from typing import List

from pydantic import BaseModel


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[str]
    plan: str
    action: str
    action_input: str
