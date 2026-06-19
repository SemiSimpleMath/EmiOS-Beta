from typing import List
from pydantic import BaseModel, Field


class ChecklistItem(BaseModel):
    """One checkpoint of the node you own. Once written, its `name` is FIXED — re-use it verbatim
    every turn; the only thing that changes is `status` (and `evidence` when you close it). To add a
    genuinely new checkpoint, append an item with an EMPTY id; to keep an existing one, echo its id."""
    id: str = Field(default="", description="Echo the [id] shown for an existing checkpoint; EMPTY for a NEW one.")
    name: str = Field(description="Short, STABLE name of the checkpoint. NEVER re-word it once written.")
    status: str = Field(default="todo", description="todo | in_progress | done | abandoned")
    evidence: str = Field(default="", description="On done: a brief note / source proving you completed THIS checkpoint.")


class Finding(BaseModel):
    """A real, answer-relevant piece of data worth preserving as a durable pod — names, prices,
    specs, a confirmed fact. NOT navigation, snapshots, or intermediate scrapes."""
    unit: str = Field(description="Short STABLE lowercase id for the THING this finding is about (re-emit the same unit to UPDATE its pod, not duplicate).")
    one_liner: str = Field(description="Brief headline of the finding.")
    body: str = Field(description="The full finding — all the real data worth keeping for the final answer.")
    source_refs: List[str] = Field(default_factory=list, description="Source URL(s).")
    tags: List[str] = Field(default_factory=list, description="A few short topic tags.")


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[ChecklistItem] = Field(
        default_factory=list,
        description=("Your checkpoints for THIS node. Echo existing items (with their id, name "
                     "verbatim) and add new ones (empty id). Only change an item's status; never "
                     "re-word its name. Mark done only AFTER its result is in recent history."),
    )
    plan: str = Field(description="Step-by-step outline of what remains.")
    action: str = Field(description="The single tool/agent to call now, or return_control.")
    action_input: str = Field(description="JSON object (as a string) of the tool's args; prose for an agent; empty for return_control.")
    findings_to_pod: List[Finding] = Field(
        default_factory=list,
        description=("0+ real findings discovered THIS turn worth keeping toward the final answer "
                     "(each becomes a durable pod). Leave empty unless this turn produced real data; "
                     "never pod navigation/snapshots/intermediate scrapes."),
    )
