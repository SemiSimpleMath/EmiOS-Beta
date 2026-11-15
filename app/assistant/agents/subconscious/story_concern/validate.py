"""The concern story's rules (agent_runtime/correction.py). Moved from brain_stories.concern_problems (2026-11-15). The
agent_input is brain_stories.concern_input: the `concern` with its latest `plan` (None before any decision).

Refused: an empty state, story or fate, and anything proposed when the latest decision is not act_now: nothing is
proposed for a hold.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    decision = (agent_input["concern"]["plan"] or {}).get("decision")
    out = [Problem(why=f"the {f} is empty", field=f) for f in ("state", "story", "fate")
           if not str(answer.get(f) or "").strip()]
    proposed = [p for p in answer.get("proposed") or [] if str(p).strip()]
    if decision != "act_now" and proposed:
        out.append(Problem(why=f"the decision is {decision or 'not made'}: nothing is proposed now, so proposed stays "
                               f"empty", field="proposed", value=proposed))
    return out
