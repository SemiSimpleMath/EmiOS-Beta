from typing import List

from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    reasoning: str = Field(
        description="One sentence explaining why you chose this action.",
    )
    acted_on_item_ids: List[str] = Field(
        default_factory=list,
        description="Item IDs from actionable_items that this action addresses.",
    )
    no_op_tf: bool = Field(
        default=False,
        description="Set true when there is nothing to dispatch this tick.",
    )
    handoff_tf: bool = Field(
        default=False,
        description=(
            "Set true to dispatch an action via the switchboard. "
            "This covers all dispatches: manager delegation, ticket creation, "
            "device control, notifications, etc."
        ),
    )
    switchboard_task: str = Field(
        default="",
        description="Task description for the switchboard. Be specific about what to do.",
    )
    switchboard_information: str = Field(
        default="",
        description="Supporting context and constraints for the switchboard.",
    )
