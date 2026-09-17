"""Output contract for the dayflow WORK FINALIZER.

A node of a work object made ONE tool call and that call came back. The finalizer reads the
result — the whole account of what the tool did, including whatever went wrong inside it — and
says what it means for the plan. One verdict per call.

The result is the input. Every tool returns the same shape, so the same judgment works whether
the node ran the work team, put a question to the user, or anything added later. A worker's
answer already narrates its own partial failures ("could not access the school account, so no
grade was read"), which is why the failure MODE is readable here and nowhere else: the steward
and the architect deal in top-level nodes, this reads what actually happened inside one.

Two of the four verdicts are for a call that returned, two for a call that failed:

    returned   proceed   the work counts, the plan still fits
               amend     the work counts, but the result changes the plan

    failed     replan    something specific can be different next time — name it
               blocked   nothing can be different; the step cannot be made to work

`replan` is the rare one. Retrying a step whose circumstances have not changed reproduces the
same error — that is exactly how one broken argument became twenty-two identical attempts in
two hours. So a replan has to name the thing that will be different, and "try again" is not a
thing that will be different.

It judges one node; work_finalizer_node writes that verdict to the graph, and its reach is
THE NODE IT JUDGED. Whether the GOAL is achieved, moot, or should be dropped is the steward's
call — it sees every work object each run, where this sees one node.
"""
from typing import List

from pydantic import BaseModel, Field, model_validator

_VERDICTS = {"proceed", "amend", "replan", "blocked"}


class AgentForm(BaseModel):
    reasoning: str = Field(
        description="One or two sentences: what the node's result actually was, and what it means for "
        "the goal. If the result suggests the GOAL is finished or has become pointless, say so here — "
        "the steward reads it and decides; you do not end work objects.")
    verdict: str = Field(
        description="Exactly one of:\n"
        "'proceed' — the call returned and the result is on-plan: the node's work genuinely counts and "
        "the rest of the existing plan still fits. This is the COMMON case.\n"
        "'amend' — the call returned, but the result changes the plan: it makes some existing steps "
        "moot or wrong, and/or reveals new steps are needed.\n"
        "'replan' — the call FAILED and something specific and nameable can be different next time "
        "(a credential re-authorised, a missing detail obtained first, a different route to the same "
        "end). RARE.\n"
        "'blocked' — the call FAILED and nothing can be different: the step cannot be made to work as "
        "asked. Say why in reasoning; the steward decides what becomes of the goal.")
    amend_intent: str = Field(
        default="",
        description="REQUIRED when verdict='amend': the revised intent in plain language — what the plan "
        "should now do given this result (e.g. 'the unit is under warranty, so drop finding a contractor "
        "and instead contact the manufacturer about a warranty service'). The architect turns this into "
        "graph changes; do NOT design nodes yourself.")
    replan_instruction: str = Field(
        default="",
        description="REQUIRED when verdict='replan': what must be DIFFERENT for this step to succeed, and "
        "how the step should change. Name the precondition concretely — 'the Google account needs "
        "re-authorising first, then the same lookup works' — not 'try again'. If you cannot name "
        "something that will actually differ, the verdict is 'blocked', not 'replan'.")
    abandon_node_ids: List[str] = Field(
        default_factory=list,
        description="OPTIONAL hint for verdict='amend': node_ids of existing nodes this result makes moot "
        "(the architect prunes them). Leave empty if unsure.")

    @model_validator(mode="after")
    def _check(self):
        if self.verdict not in _VERDICTS:
            raise ValueError(f"verdict must be one of {_VERDICTS}")
        if self.verdict == "amend" and not str(self.amend_intent or "").strip():
            raise ValueError("amend_intent is required when verdict='amend'")
        if self.verdict == "replan" and not str(self.replan_instruction or "").strip():
            raise ValueError(
                "replan_instruction is required when verdict='replan' — a replan must name what "
                "will be different, otherwise the step repeats and so does its error")
        return self
