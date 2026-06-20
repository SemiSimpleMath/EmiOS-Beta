from typing import List
from pydantic import BaseModel, Field


class ChecklistItem(BaseModel):
    """One surface (whole step delegated to one specialist manager) in your PRIVATE running plan. This
    is scratch for your own reasoning — it is NOT the graph; the tasks you delegate are tracked as graph
    nodes automatically. List the surfaces and mark each done as its result comes in."""
    text: str = Field(description="Short, outcome-based description of the surface / step.")
    status: str = Field(default="todo", description="todo | doing | done | abandoned")


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[ChecklistItem] = Field(
        default_factory=list,
        description=("Your PRIVATE running plan of the SURFACES this node needs — each a whole step you "
                     "DELEGATE to one specialist manager. This is scratch for your own reasoning; it is "
                     "NOT the graph (your delegations are tracked as nodes automatically). List the "
                     "surfaces and mark each done as its result comes in. A single-surface node needs no "
                     "checklist."),
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
