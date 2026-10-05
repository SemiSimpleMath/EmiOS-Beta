from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class EntryChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: str = Field(description="The id of an entry on the list now, or \"new\" for an entry you add.")
    action: Literal["new", "modify", "cancel"] = Field(
        description="new: an outcome the concern needs that nothing on the list covers. modify: an entry that still "
                    "belongs to the concern but whose outcome, part or time changed. cancel: an entry that no longer "
                    "serves the concern.")
    what: str = Field(description="For new and modify: the outcome wanted, with its deadline and every hard "
                                  "constraint. Empty for cancel.")
    assistant_part: str = Field(description="For new and modify: the assistant's own act (a home device, a calendar "
                                            "entry, a todo, a lookup, an email with the owner's go) or what it tells, "
                                            "reminds or asks the owner. Empty for cancel.")
    when: str = Field(description="For new and modify: when it makes sense (a local date and time, or now). Empty "
                                  "for cancel.")
    why: str = Field(description="Why, in one or two sentences; for a modify or a cancel, what changed.")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["act_now", "hold", "no_action"] = Field(
        description="act_now: the concern needs an entry it does not have, or an entry changed; give them in "
                    "entries. hold: nothing until a date; give it. no_action: nothing needs doing now (handled, work "
                    "in progress covers it, the owner has it in hand, or it is only to know).")
    hold_until: Optional[str] = Field(
        default=None, description="For hold: the local date and time to look at it again, as YYYY-MM-DD HH:MM. "
                                  "Empty otherwise.")
    entries: List[EntryChange] = Field(description="Only the changes to the concern's entries: new ones, and those "
                                                   "you modify or cancel. An entry you leave out stays as it is.")
    why: str = Field(description="What the decision rests on, in one to three sentences.")
