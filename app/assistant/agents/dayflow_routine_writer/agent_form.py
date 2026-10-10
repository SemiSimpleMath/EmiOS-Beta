from typing import List

from pydantic import BaseModel, Field


class Placement(BaseModel):
    time: str = Field(description="When it applies: a clock time such as '9:00 PM' or '7:30 AM', or 'all day'.")
    until: str = Field(description="For a clock time: the clock time after which this no longer matters today, at "
                                   "most about two hours after its time unless what it asks plainly needs longer, "
                                   "such as '11:00 PM'. Empty for 'all day', which matters until midnight.")


class Judgment(BaseModel):
    belief_key: str = Field(description="The belief's key: what follows 'belief:' in its [belief:...] marker, "
                                        "e.g. B10.")
    why: str = Field(description="Whether today meets the belief's day, conditions and schedule, as the facts "
                                 "about today and the schedule state.")
    applies_today: bool = Field(description="True when the reason above says the belief applies today.")
    outcome: str = Field(description="When it applies: what it asks for today, as an outcome in one line, in words "
                                     "(e.g. 'The trash is out at the curb before bed'). Empty when it does not "
                                     "apply.")
    times: List[Placement] = Field(description="Each time it applies today; empty when it does not apply.")


class AgentForm(BaseModel):
    judgments: List[Judgment] = Field(description="One judgment per approved belief.")
