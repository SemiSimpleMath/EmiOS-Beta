from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(description="The calendar item's label, as given (I1, I2, ...).")
    decision: Literal["needed", "not_needed", "done_elsewhere"] = Field(
        description="needed: today needs the assistant to do something. not_needed: nothing today. "
                    "done_elsewhere: the owner or someone else already did it today.")
    who_acts: str = Field(description="For needed: the assistant, the owner, or another person by name. Empty "
                                      "otherwise.")
    assistant_part: str = Field(description="For needed: what the assistant does, as the outcome: the act itself, or "
                                            "tell, remind, ask or check, with what. Empty otherwise.")
    when: str = Field(description="For needed: the local time it makes sense (e.g. 6:00 PM), or now. Empty otherwise.")
    covers: List[str] = Field(description="Filled only on a needed item: the labels of the other items this one task "
                                          "also covers. Every other decision leaves it empty; a covered item names the "
                                          "item that covers it in why.")
    why: str = Field(description="Why, in one or two sentences. For an item another task covers, name that item.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: List[Decision] = Field(description="One decision for every calendar item, by its label.")
