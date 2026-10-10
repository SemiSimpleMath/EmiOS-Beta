from typing import List

from pydantic import BaseModel, Field


class Placement(BaseModel):
    lines: List[str] = Field(description="The labels of the plan's lines this placement is made of (L1, L2, ...): "
                                         "every line about the same thing at the same part of the day. Every line "
                                         "is in exactly one placement.")
    continues: str = Field(description="The label of today's concern this placement is (P1, P2, ...) when one of "
                                       "them is about the same thing; empty when none is.")
    outcome: str = Field(description="What the placement asks for today, as an outcome in one line, in words: what "
                                     "is true when it has been done.")
    time: str = Field(description="When it applies: a clock time such as '9:00 PM' or '7:30 AM', or 'all day'.")
    until: str = Field(description="For a clock time: the clock time after which it no longer matters today, such "
                                   "as '11:00 PM'. Empty for 'all day'.")


class AgentForm(BaseModel):
    placements: List[Placement] = Field(description="One placement per thing today holds.")
