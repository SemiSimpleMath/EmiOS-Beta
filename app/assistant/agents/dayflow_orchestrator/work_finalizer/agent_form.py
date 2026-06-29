"""Output contract for the dayflow WORK FINALIZER.

A worker just completed ONE node of a work object and produced a RESULT. The finalizer reads that result
as EVIDENCE and decides how it bears on the WHOLE work object: PROCEED (the plan still holds), AMEND (the
result changes the plan — the architect revises it) or RESOLVE (the goal itself is settled). One verdict
per call. In shadow mode the verdict is only logged; once authoritative it drives node-close / re-plan /
work-object close.
"""
from typing import List

from pydantic import BaseModel, Field, model_validator

_VERDICTS = {"proceed", "amend", "resolve"}
_RESOLUTIONS = {"close", "abandon"}


class AgentForm(BaseModel):
    reasoning: str = Field(
        description="One or two sentences: what the completed node's result actually was, and what it means "
        "for the goal.")
    verdict: str = Field(
        description="Exactly one of: 'proceed' (the result is on-plan — the node's work genuinely counts "
        "and the rest of the existing plan still fits; nothing to change — this is the COMMON case); "
        "'amend' (the result changes the plan — it makes some existing steps moot/wrong and/or reveals new "
        "steps are needed); 'resolve' (the result settles the whole GOAL — it is achieved, or now moot).")
    amend_intent: str = Field(
        default="",
        description="REQUIRED when verdict='amend': the revised intent in plain language — what the plan "
        "should now do given this result (e.g. 'the unit is under warranty, so drop finding a contractor "
        "and instead contact the manufacturer about a warranty service'). The architect turns this into "
        "graph changes; do NOT design nodes yourself.")
    abandon_node_ids: List[str] = Field(
        default_factory=list,
        description="OPTIONAL hint for verdict='amend': node_ids of existing nodes this result makes moot "
        "(the architect prunes them). Leave empty if unsure.")
    resolve_disposition: str = Field(
        default="",
        description="REQUIRED when verdict='resolve': 'close' (the goal is genuinely achieved) or 'abandon' "
        "(the goal is moot / cannot be achieved).")

    @model_validator(mode="after")
    def _check(self):
        if self.verdict not in _VERDICTS:
            raise ValueError(f"verdict must be one of {_VERDICTS}")
        if self.verdict == "amend" and not str(self.amend_intent or "").strip():
            raise ValueError("amend_intent is required when verdict='amend'")
        if self.verdict == "resolve" and self.resolve_disposition not in _RESOLUTIONS:
            raise ValueError("resolve_disposition must be 'close' or 'abandon' when verdict='resolve'")
        return self
