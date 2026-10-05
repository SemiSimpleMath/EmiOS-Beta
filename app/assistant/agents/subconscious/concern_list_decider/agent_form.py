from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field


class EntryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: str = Field(description="The id of an entry on the list now, or \"new\" for an entry you add.")
    action: Literal["keep", "modify", "cancel", "new"] = Field(
        description="keep: it still serves the concern as it stands. modify: what it needs changed. cancel: it no "
                    "longer serves the concern. new: an entry the concern needs that nothing on the list covers.")
    what: str = Field(description="For modify and new: the outcome wanted. Empty for keep and cancel.")
    who_acts: str = Field(description="For modify and new: the assistant, the owner, or another person by name. "
                                      "Empty for keep and cancel.")
    assistant_part: str = Field(description="For modify and new: what the assistant does: the act itself, or tell, "
                                            "remind, ask or check, with what. Empty for keep and cancel.")
    when: str = Field(description="For modify and new: when it makes sense (a local date and time, or now). Empty "
                                  "for keep and cancel.")
    why: str = Field(description="Why, in one or two sentences; for a modify or a cancel, what changed.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: List[EntryAction] = Field(description="One answer for every entry on the list now, by its id, then any "
                                                   "new entries.")
