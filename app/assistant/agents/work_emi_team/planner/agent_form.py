from typing import List
from pydantic import BaseModel, Field


class ChecklistItem(BaseModel):
    """One surface of THIS node — a whole step you hand to one specialist manager. The checklist
    round-trips through the GRAPH: the render shows each existing surface with its `id`; ECHO that id for
    existing items, leave it EMPTY for a new one. `text` is fixed once written. Mark exactly ONE
    in_progress at a time — the manager you delegate this turn nests UNDER that in-progress item."""
    id: str = Field(default="", description="Echo the [id] shown in YOUR CHECKLIST for an existing item; EMPTY for a new one.")
    text: str = Field(description="Short, outcome-based description of the surface / step. Fixed once written.")
    status: str = Field(default="todo", description="todo | in_progress | incomplete (paused, resume later) | done | abandoned")
    evidence: str = Field(default="", description="On done: a brief note / result proving this surface is complete.")


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[ChecklistItem] = Field(
        default_factory=list,
        description=("The SURFACES this node needs — each a whole step you DELEGATE to one specialist "
                     "manager. Mirrored to the graph as subtask nodes: echo existing items (id + text "
                     "verbatim), add new ones (empty id), and mark exactly ONE in_progress at a time. The "
                     "manager you delegate this turn nests UNDER the in_progress item. A single-surface "
                     "node needs no checklist — just delegate it."),
    )
    findings: List[str] = Field(
        default_factory=list,
        description=("Concrete results/answers you have produced for THIS node — each is recorded "
                     "DURABLY on the node (the finalizer builds the node's answer from these, and "
                     "other nodes can reuse them). Add a result the turn after it appears in recent "
                     "history; for a direct answer, write it as you give it; and ALWAYS write your "
                     "final result here on the same turn you return_control. Don't repeat ones "
                     "already recorded."),
    )
    info_for_others: List[str] = Field(
        default_factory=list,
        description=("0+ things you noticed THIS turn that could matter to OTHER agents on the overall "
                     "goal but lie OUTSIDE your own task — a key fact, a useful source/URL, or something "
                     "that changes the premise of the whole goal. Each becomes a shared note visible to "
                     "every agent and the curator. Usually empty; do NOT put your own task's findings "
                     "here, and don't repeat items already shared."),
    )
    plan: str = Field(description="Step-by-step outline of what remains.")
    action: str = Field(description="The single tool/agent to call now, or return_control.")
    action_input: str = Field(description="JSON object (as a string) of the tool's args; prose for an agent; empty for return_control.")
