from typing import List
from pydantic import BaseModel, Field


class Finding(BaseModel):
    """A real, answer-relevant piece of data worth preserving as a durable pod.

    Emit one when you've DISCOVERED real data (names, addresses, phones, prices, hours, URLs,
    a confirmed fact) — NOT for navigation, page snapshots, or intermediate scrapes that only
    helped you get there. The `one_liner` is the headline you'd show in a list; the `body` holds
    the full data so it can be dropped from your working notes and recovered later."""
    unit: str = Field(
        description=(
            "Short, STABLE lowercase id for the THING this finding is about (the company, product, "
            "place), e.g. 'mcmaster' or 'white_mechanical'. Refining a finding for the same unit "
            "UPDATES its existing note instead of creating a duplicate — so use the same unit id "
            "every time you add detail about it."
        ),
    )
    one_liner: str = Field(description="Brief headline of the finding (shown in the notebook index).")
    body: str = Field(description="The full finding — all the real data worth keeping for the final answer.")
    source_refs: List[str] = Field(default_factory=list, description="Source URL(s) this came from.")
    tags: List[str] = Field(default_factory=list, description="A few short topic tags, e.g. ['del_taco','irvine'].")


class AgentForm(BaseModel):
    what_i_am_thinking: str
    checklist: List[str]
    progress: str
    plan: str
    action: str
    action_input: str
    findings_to_pod: List[Finding] = Field(
        default_factory=list,
        description=(
            "0+ real findings you discovered this turn that are worth keeping toward the final "
            "answer. Each becomes a durable note you no longer need to re-hold in detail — afterward "
            "you'll see only its one_liner. Leave empty unless this turn produced real data. Never "
            "pod navigation, snapshots, or intermediate scrapes."
        ),
    )
