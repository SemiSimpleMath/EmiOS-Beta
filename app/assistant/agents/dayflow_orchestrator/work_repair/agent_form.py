"""Output contract for the dayflow WORK REPAIR adjudicator.

Decides what to do about a work object STUCK on a FAILED node, so its goal can move forward (or be
honestly closed) instead of sitting 'active' forever. One disposition per call; the deterministic
apply (work_repair_apply.apply_adjudication) carries it out on the failed node(s).
"""
from pydantic import BaseModel, Field, model_validator

_DISPOSITIONS = {"escalate", "retry", "abandon_goal"}


class AgentForm(BaseModel):
    reasoning: str = Field(description="One or two sentences: what failed, why, and why this disposition.")
    disposition: str = Field(
        description="Exactly one of: 'escalate' (the user must DO or PROVIDE this — re-issue the step as "
        "an ask to them); 'retry' (transient failure, or the info the step needs is now present — just "
        "re-run it); 'abandon_goal' (the goal cannot be achieved and isn't worth re-issuing — drop the "
        "whole work object).")
    ask: str = Field(
        default="",
        description="REQUIRED when disposition='escalate': the request to the user, in first person, "
        "stating clearly what they must do or provide (shown to them verbatim). E.g. \"I can't book the "
        "brake inspection myself — could you book it before June 26 and let me know once it's done?\"")

    @model_validator(mode="after")
    def _check(self):
        if self.disposition not in _DISPOSITIONS:
            raise ValueError(f"disposition must be one of {_DISPOSITIONS}")
        if self.disposition == "escalate" and not str(self.ask or "").strip():
            raise ValueError("ask is required when disposition='escalate'")
        return self
