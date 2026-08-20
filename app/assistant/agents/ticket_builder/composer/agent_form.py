from enum import Enum
from pydantic import BaseModel, Field


class TicketKind(str, Enum):
    notify = "notify"
    advice = "advice"
    decision = "decision"


class AgentForm(BaseModel):
    reasoning: str = Field(
        description="One sentence on why this ticket format is appropriate.",
    )
    ticket_kind: TicketKind = Field(
        description="Ticket style: notify (FYI), advice (gentle nudge), decision (reply required).",
    )
    suggestion_type: str = Field(
        description="Short category tag, e.g. logistics, wellness, scheduling.",
    )
    title: str = Field(
        description="Short, natural title shown in ticket UI.",
    )
    message: str = Field(
        description="Natural-language message body shown to the user.",
    )
