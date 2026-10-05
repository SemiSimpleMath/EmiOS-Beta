from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(description="The calendar item's label, as given (I1, I2, ...).")
    decision: Literal["needed", "not_needed", "done_elsewhere"] = Field(
        description="needed: today needs the assistant to do something. not_needed: nothing today. "
                    "done_elsewhere: the owner or someone else already did it today.")
    assistant_part: str = Field(description="For needed: what the assistant does: its own act (a home device, a "
                                            "calendar entry, a todo, a lookup) or what it tells, reminds or asks the "
                                            "owner. Empty otherwise.")
    when: str = Field(description="For needed: the local time it makes sense (e.g. 6:00 PM), or now. Empty otherwise.")
    covers: List[str] = Field(description="Filled only on a needed item: the labels of the other items this one task "
                                          "also covers. Every other decision leaves it empty; a covered item names the "
                                          "item that covers it in why.")
    why: str = Field(description="Why, in one or two sentences. For an item another task covers, name that item.")
    new_since: List[str] = Field(description="Only for an item already on the list: the labels (M, W, R, C, E, B) of what "
                                             "you read that is new since its entry was decided and changes it. Empty "
                                             "for an item not yet on the list.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: List[Decision] = Field(description="A decision for every item not yet on the list, and one for each "
                                                  "item on the list that something new changes. An item on the list "
                                                  "you leave out keeps its entry as it is.")
