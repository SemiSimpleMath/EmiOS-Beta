"""Output schema for weekly_meal::context_planner.

Standard Planner shape: reasoning trace + checklist + action.

The planner's job is to PRODUCE the relevant context for the
weekly_meal_planner downstream — actively assemble what the proposer
needs for THIS specific week's proposal. That includes the audience for
each day, per-person dietary constraints, situational signals from the
noticer's concerns_register, location, and any precedent (past plans,
recent feedback) that should still be respected.

The planner is a producer / assembler, not a gatekeeper deciding
whether seed-suffices. Frame the work as 'what does the proposer need?
Let me build it,' not 'do I have enough?'

action='return_control' hands off to weekly_meal_planner when the
bundle is complete for this specific proposal — not when the seed
happens to look rich.

Cold-start note: for a week with no precedent (no past plan.weekly_meals
pod, no recent feedback.comment pods on prior meals), there is naturally
less to produce — but the producer-disposition still applies: actively
assemble what the proposer needs, even if less work is required.
"""
from typing import List

from pydantic import BaseModel


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[str]
    plan: str
    action: str
    action_input: str
