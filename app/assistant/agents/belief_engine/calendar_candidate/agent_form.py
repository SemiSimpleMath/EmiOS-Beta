from typing import List

from pydantic import BaseModel, Field


class Decision(BaseModel):
    belief_key: str = Field(description="The belief's key, exactly as shown (B<number>).")
    candidate: bool = Field(description="True when this belief might belong on the day-planning calendar some day.")
    when: str = Field(
        default="",
        description="For a candidate: when it would appear on the calendar, in plain words ('every Wednesday "
                    "night', 'December 25 each year', 'the second business day of each month'). Empty otherwise.")
    reason: str = Field(description="One short sentence: why it is, or is not, calendar material.")


class AgentForm(BaseModel):
    decisions: List[Decision] = Field(description="One decision for every belief given, each exactly once.")
