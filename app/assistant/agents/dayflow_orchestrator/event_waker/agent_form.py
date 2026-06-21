"""Output contract for the EVENT WAKER (Increment 3 of the dayflow work-object cutover).

The graph enforces dependency + time gates deterministically (is_ready). The ONE thing it cannot
compute is whether an external event has happened — did the reply arrive? The waker does only that:
given the tasks parked on an external event + the recent intake, it returns the ones whose awaited
event has clearly arrived, with the content the resuming worker needs. Mirrors the state_mover's
conservative "wake signal triggered -> promote" doctrine, node-native.
"""
from typing import List

from pydantic import BaseModel, Field


class EventMatch(BaseModel):
    task_id: str = Field(description="The parked task id EXACTLY as shown (work_id::node_id).")
    evidence: str = Field(
        description="The intake content that satisfies the wait — the gist of what arrived (e.g. the "
        "reply's substance) — so the worker resuming this task has what it needs to act.")


class AgentForm(BaseModel):
    waker_summary: str = Field(description="One line on what you matched, or that nothing matched.")
    matches: List[EventMatch] = Field(
        default_factory=list,
        description="ONLY the parked tasks whose awaited event has clearly arrived in the intake. Omit "
        "anything uncertain — it stays parked and you will see it again next cycle.")
