from typing import List

from pydantic import BaseModel, Field


class Placement(BaseModel):
    belief_key: str = Field(description="The belief's key: what follows 'belief:' in its [belief:...] marker, "
                                        "e.g. B10.")
    time: str = Field(description="When it applies today: a clock time such as '9:00 PM' or '7:30 AM', "
                                  "or 'all day'.")


class AgentForm(BaseModel):
    placements: List[Placement] = Field(
        description="One entry per belief that applies today, per time it applies; none for a belief that "
                    "does not apply today.")
