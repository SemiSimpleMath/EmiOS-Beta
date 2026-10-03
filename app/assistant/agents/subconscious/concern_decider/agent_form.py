from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["act_now", "hold", "no_action"] = Field(
        description="act_now: things should be done about it now. hold: nothing until a date; give it. "
                    "no_action: nothing needs doing now (already handled, work in progress covers it, the owner "
                    "has it in hand, or it is only to know).")
    hold_until: Optional[str] = Field(
        default=None, description="For hold: the local date and time to look at it again, as YYYY-MM-DD HH:MM. "
                                  "Empty otherwise.")
    todo: List[str] = Field(
        description="For act_now: the things that should be done, one per entry, each stated as the outcome "
                    "wanted. Empty otherwise.")
    why: str = Field(description="Why this decision now, in one to three sentences.")
