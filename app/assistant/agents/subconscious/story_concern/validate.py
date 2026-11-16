"""The concern story's rules (agent_runtime/correction.py). Moved from brain_stories.concern_problems (2026-11-15). The
agent_input is brain_stories.concern_input: the `concern` with its latest `plan` (None before any decision).

Refused: an empty state, story or fate, and anything proposed when the latest decision is not act_now: nothing is
proposed for a hold.
"""
from app.assistant.agent_runtime.correction import Problem

_DECISIONS = {"hold": "to hold", "no_action": "no action"}  # the decision as the correction says it, in words


def validate(answer, agent_input):
    decision = (agent_input["concern"]["plan"] or {}).get("decision")
    out = [Problem(why=f"the {f} is empty", field=f) for f in ("state", "story", "fate")
           if not str(answer.get(f) or "").strip()]
    proposed = [p for p in answer.get("proposed") or [] if str(p).strip()]
    if decision != "act_now" and proposed:
        said = _DECISIONS.get(decision, "that no decision has been made")
        out.append(Problem(why=f"the latest decision is {said}, so nothing is proposed now: proposed stays empty, "
                               f"and what is still to be done belongs in the story or the fate", field="proposed",
                           value=proposed))
    return out
