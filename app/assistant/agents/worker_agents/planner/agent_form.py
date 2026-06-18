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
    progress: List[str] = Field(
        default_factory=list,
        description=("Append-only, high-signal milestones / discoveries from THIS turn (each becomes an "
                     "Evidence node the curator may share with other agents). Usually 0-1 items. Do not "
                     "repeat items already shown."),
    )
    plan: str = Field(description="Step-by-step outline of what remains.")
    action: str = Field(description="The single tool/agent to call now, or return_control.")
    action_input: str = Field(description="JSON object (as a string) of the tool's args; prose for an agent; empty for return_control.")
