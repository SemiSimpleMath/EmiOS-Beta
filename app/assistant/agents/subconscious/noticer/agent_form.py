"""Output schema for the subconscious noticer.

The noticer looks at the long term (patterns across weeks of signals, needs a date or deadline ahead will bring,
opportunities in the family graph) and raises issues. Each issue reaches the brain's inbox as an event; the brain
decides whether it becomes a concern (owner, 2026-10-09: "noticer is supposed to look at long term things. it should
just raise issues that can then become concerns"). It writes nothing else: no concern, no belief, no question to the
owner. Until 2026-10-09 it also reinforced, resolved, escalated and forced dispositions on concerns directly, and
asked the owner questions whose answers it processed.
"""
from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field

IssueKind = Literal[
    "pattern_drift",
    "anticipated_need",
    "opportunity_external",
    "opportunity_internal",
    "schedule_collision",
    "gift_opportunity",
]


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=120, description="One line, specific.")
    kind: IssueKind
    subject: str = Field(description="The family member it is about, or 'household'.")
    what: str = Field(description="The issue in words: the pattern or the need, with its dates, and what in the "
                                  "context shows it (the signals, the calendar entry, the obligation). Copy the "
                                  "owner's words where they matter. No ids.")
    why_now: str = Field(description="Why it is worth raising now: the count reached, the lead time arriving, the date "
                                     "approaching.")


class AgentForm(BaseModel):
    """Top-level noticer output — the agent runtime expects this exact name."""
    model_config = ConfigDict(extra="forbid")
    issues: List[Issue] = Field(description="The issues worth the brain's attention, each once. Empty when nothing "
                                            "new is worth raising.")
    summary: str = Field(max_length=800, description="2-4 sentences on what you looked at and what you raised.")
