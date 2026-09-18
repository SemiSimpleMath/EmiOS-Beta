"""Output contract for the dayflow WORK FINALIZER.

A node of a work object made ONE tool call and that call came back. The finalizer reads the
result — the whole account of what the tool did, including whatever went wrong inside it — and
answers one question: was the NODE'S GOAL achieved? Everything else follows from that answer.

The tool's own status (returned cleanly / reported failure) is INPUT, never the verdict. A call
can come back clean and well-researched and achieve nothing the node was for; a call can report a
failure inside it and still have achieved what the node asked. Judge the goal, not the effort.

The output is three things, not one:

    outcome         prose, ALWAYS. What actually happened — what was found, what was not, why. This
                    is what the architect or steward reads next tick to decide what to do, so it has
                    to be a usable account, not a verdict restated.
    verdict         the routing enum. Decides what the store does with the node.
    recommendation  on every verdict except `achieved`: what should happen now, in plain language.
                    For `unrecoverable`, `next_step` types it so the runtime can route it.

The finalizer judges ONE node. Whether the GOAL is finished or moot is the steward's call — it sees
every work object each run, where this sees one node. Say so in `outcome` and the steward acts.
"""
from typing import List

from pydantic import BaseModel, Field, model_validator

VERDICTS = ("achieved", "achieved_plan_changes", "retry", "unrecoverable")
NEXT_STEPS = ("stop", "new_approach", "ask_user")


class AgentForm(BaseModel):
    outcome: str = Field(
        description="ALWAYS. The account of what happened, written for the planner who reads it next: "
        "what the node was for, what actually came back, what was found and what was not, and why. If "
        "the result means the whole GOAL is finished or has become pointless, say so plainly here — the "
        "steward reads this and decides; you do not end work objects. Failed sub-steps inside an achieved "
        "call belong here too, as provenance.")
    verdict: str = Field(
        description="Exactly one of:\n"
        "'achieved' — the node's goal was met. Sub-steps may have failed along the way; if the call came "
        "back having done what the node asked, this is the verdict. The COMMON case. Nothing further is "
        "asked of the architect.\n"
        "'achieved_plan_changes' — the goal was met, AND what came back changes the plan around it: some "
        "existing steps are now moot or wrong, or new ones are needed. Say what should change in "
        "`recommendation`.\n"
        "'retry' — the goal was NOT met, the failure is minor, and it makes sense to try again later or a "
        "little differently. `recommendation` must say WHAT will be different (a precondition, a route, "
        "a time). The node goes back to the architect's inbox.\n"
        "'unrecoverable' — the goal was NOT met and there is no recovery on this path. `next_step` says "
        "which of three things should happen instead.")
    next_step: str = Field(
        default="",
        description="REQUIRED when verdict='unrecoverable', empty otherwise. One of:\n"
        "'stop' — this line of work should not continue (the branch is moot, or the whole goal is — say "
        "which in `outcome`).\n"
        "'new_approach' — the goal still stands but this way of reaching it is wrong; the architect "
        "should plan a genuinely different route.\n"
        "'ask_user' — the way forward needs the USER: their account, their permission, their presence, "
        "a decision, or something only they know. Put the question in `question_for_user`. Contacting "
        "someone outside the household on their behalf is never a workaround for this.")
    recommendation: str = Field(
        default="",
        description="REQUIRED for every verdict except 'achieved': what should happen now, in plain "
        "language, for the architect or steward to act on. For 'retry': what will be different next time. "
        "For 'achieved_plan_changes': what the plan should now do. For 'unrecoverable': the specifics "
        "behind `next_step`. Never 'try again' with nothing named.")
    question_for_user: str = Field(
        default="",
        description="REQUIRED when next_step='ask_user': the exact question the user should be asked, "
        "self-contained, so it can be put to them as-is.")
    abandon_node_ids: List[str] = Field(
        default_factory=list,
        description="OPTIONAL hint for 'achieved_plan_changes' or next_step='new_approach': node_ids of "
        "existing nodes this result makes moot (the architect prunes them). Leave empty if unsure.")

    @model_validator(mode="after")
    def _check(self):
        v = str(self.verdict or "").strip().lower()
        ns = str(self.next_step or "").strip().lower()
        self.verdict, self.next_step = v, ns
        if v not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        if not str(self.outcome or "").strip():
            raise ValueError("outcome is required on every verdict — it is what the planner reads")
        if v == "achieved":
            if ns:
                raise ValueError("next_step must be empty when verdict='achieved'")
            return self
        if not str(self.recommendation or "").strip():
            raise ValueError(f"recommendation is required when verdict='{v}' — say what should happen")
        if v == "unrecoverable":
            if ns not in NEXT_STEPS:
                raise ValueError(f"next_step must be one of {NEXT_STEPS} when verdict='unrecoverable'")
            if ns == "ask_user" and not str(self.question_for_user or "").strip():
                raise ValueError("question_for_user is required when next_step='ask_user'")
        elif ns:
            raise ValueError(f"next_step is only for verdict='unrecoverable' (got verdict='{v}')")
        return self
