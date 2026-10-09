from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field

# References are labels of this call (I1, O1, W3, M2, R1, C1, E2, B4, K1, D) in their own fields; code maps them to
# the objects (docs/design/objects_and_projections_2026-10-08.md). The prose fields are words only.


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(description="The calendar item's label, as given (I1, I2, ...).")
    decision: Literal["needed", "not_needed", "done_elsewhere"] = Field(
        description="needed: today needs the assistant to do something. not_needed: nothing today. "
                    "done_elsewhere: the owner or someone else already did it today.")
    assistant_part: str = Field(description="For needed: what the assistant does, in words: its own act (a home "
                                            "device, a calendar entry, a todo, a lookup) or what it tells, reminds or "
                                            "asks the owner. Empty otherwise.")
    when: str = Field(description="For needed: the local time it makes sense (e.g. 6:00 PM), or now. Empty otherwise.")
    covers: List[str] = Field(description="Filled only on a needed item: the labels of the other items this one task "
                                          "also covers. Every other decision leaves it empty.")
    why: str = Field(description="Why, in one or two sentences of words. Name things in words and dates; no labels.")
    relies_on: List[str] = Field(description="The labels of what the reason rests on (chat M, work W, reminders R, "
                                             "concerns C, concern entries E, the brain's decisions B, schedule lines "
                                             "K, the standing directives D, other items I, your own entries O). Empty "
                                             "when it rests on the item alone.")


class OwnEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: str = Field(description='"new" for a new entry; the label of one of your own entries (O1, O2...) to '
                                   'modify or cancel.')
    action: Literal["new", "modify", "cancel"]
    what: str = Field(description="The outcome wanted, with its time, in words. Empty for a cancel.")
    assistant_part: str = Field(description="What the assistant does, in words: its own act or what it tells, "
                                            "reminds or asks the owner. Empty for a cancel.")
    when: str = Field(description="The local time it makes sense (e.g. 6:00 PM), or now. Empty for a cancel.")
    why: str = Field(description="Why, in one or two sentences of words; no labels.")
    concern: str = Field(description="The label (C…) of the open concern this entry serves, when it serves one: "
                                     "its work then belongs to that concern, and its outcome reaches it. Empty when "
                                     "it serves no concern.")
    relies_on: List[str] = Field(description="The labels of what prompted it: schedule lines (K), chat (M), work "
                                             "(W), reminders sent (R), concerns (C), concern entries (E), the brain's "
                                             "decisions (B), the standing directives (D).")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: List[Decision] = Field(description="A decision for every item not yet on the list, and one for each "
                                                  "item on the list whose entry the day now needs changed. An item on "
                                                  "the list you leave out keeps its entry as it is.")
    own_entries: List[OwnEntry] = Field(description="Your own entries for the day: something today needs that no item "
                                                    "asks for, and changes to your own entries made earlier. Empty when "
                                                    "nothing is needed.")
