from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field

# References are labels of this call (K events, C concerns, B the brain's decisions) in their own fields; code maps
# them to objects (docs/design/objects_and_projections_2026-10-08.md). The prose fields are words only.


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str = Field(description='The event\'s label (K1, K2...), or "new" for an event the day revealed that is '
                                   'not on the record.')
    title: str = Field(description="For a new event: what it is, in words. Empty otherwise.")
    start: str = Field(description="For a new event, or a moved one: its local start time (e.g. 6:30 PM). Empty "
                                   "otherwise.")
    end: str = Field(description="Its local end time when known (e.g. 7:00 PM). Empty otherwise.")
    state: Literal["planned", "happened", "moved", "cancelled", "did_not_happen"] = Field(
        description="planned: still planned, not yet shown to have happened. happened. moved: to the start given. "
                    "cancelled: it will not take place. did_not_happen: its time came and it did not take place.")
    basis: Literal["direct", "inferred"] = Field(
        description="direct: something you read says so. inferred: nothing says so, but signs you read point to it.")
    relies_on: List[str] = Field(description="The labels of what shows it: concerns (C), the brain's decisions (B), "
                                             "other events (K).")
    note: str = Field(description="What shows it, in words and times; no labels.")


class Milestone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: str = Field(description="The local time it happened (e.g. 5:12 PM).")
    description: str = Field(description="What happened, in words; no labels.")
    basis: Literal["direct", "inferred"]
    relies_on: List[str] = Field(description="The labels of what shows it: concerns (C), the brain's decisions (B), "
                                             "events (K).")


class AgentForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: List[Change] = Field(description="Each event whose state the day as now known changes, and each new "
                                              "event. An event you leave out keeps its state.")
    milestones: List[Milestone] = Field(description="Things that happened since the milestones already recorded. "
                                                    "Empty when none.")
    day_theme: str = Field(description="One or two sentences: what kind of day it is, in words.")
    current_status: str = Field(description="One short line: what the owner is doing now, as far as is known.")
