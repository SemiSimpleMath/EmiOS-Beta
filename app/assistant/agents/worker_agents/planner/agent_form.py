from typing import List
from pydantic import BaseModel, Field


class ChecklistItem(BaseModel):
    """One subtask of the node this planner owns. The checklist round-trips through
    the GRAPH: the render node shows each existing subtask with its `id`; ECHO that
    id for existing items, leave it EMPTY for a new item you are adding this turn."""
    id: str = Field(default="", description="Echo the [id] shown in YOUR CHECKLIST for an existing item; EMPTY for a new one.")
    text: str = Field(description="Short, stable, outcome-based description of the subtask.")
    status: str = Field(default="todo", description="todo | doing | done | abandoned")
    evidence: str = Field(default="", description="On done: a brief note / result / pod_ref proving it.")


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[ChecklistItem] = Field(
        default_factory=list,
        description=("Your durable subtasks for THIS node. Echo existing items (with their id) and "
                     "add new ones (empty id). Mark an item done only AFTER its result appears in "
                     "recent history — never in the same turn as the action that would complete it."),
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
