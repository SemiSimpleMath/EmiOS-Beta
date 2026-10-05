from typing import List

from pydantic import BaseModel, Field


class Judgment(BaseModel):
    belief_key: str = Field(description="The belief's key: what follows 'belief:' in its [belief:...] marker, "
                                        "e.g. B10.")
    why: str = Field(description="Whether today meets the belief's day, conditions and schedule, as the facts "
                                 "about today and the schedule state.")
    applies_today: bool = Field(description="True when the reason above says the belief applies today.")
    times: List[str] = Field(description="When it applies today: clock times such as '9:00 PM' or '7:30 AM', "
                                         "or ['all day']; empty when it does not apply.")


class AgentForm(BaseModel):
    judgments: List[Judgment] = Field(description="One judgment per approved belief.")
