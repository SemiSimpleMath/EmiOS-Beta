"""Output schema for meal_research::planner.

Standard Planner shape: emits an action (a tool name like ask_kg /
pod_search) with action_input, or 'return_control' when the research
question has been answered well enough for the calling planner.
"""
from typing import List

from pydantic import BaseModel


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[str]
    plan: str
    action: str
    action_input: str
