"""The calendar candidate's rules (agent_runtime/correction.py). Moved from intake.calendar._problems (2026-11-15).
The agent_input is the page intake.calendar.tag_calendar_candidates makes: `beliefs`, each with its key `id`.

Refused: a key that is not one of the beliefs given, a belief decided twice or not at all, and a candidate with no
`when`. The form refuses an answer with no decisions list. A missing `when` is corrected decision by decision.
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"decisions": "belief_key"}


def validate(answer, agent_input):
    keys = [b["id"] for b in agent_input["beliefs"]]
    out, seen = [], []
    for d in answer.get("decisions") or []:
        key = str(d.get("belief_key") or "").strip()
        if key not in keys:
            out.append(Problem(why="it is not one of the beliefs given", field="decisions", value=key,
                               structural=True))
            continue
        seen.append(key)
        if d.get("candidate") and not str(d.get("when") or "").strip():
            out.append(Problem(why="a candidate needs `when`: when it would appear on the calendar, in plain words",
                               field="decisions", item=key, at="when", value=d.get("when")))
    for key in keys:
        if seen.count(key) != 1:
            out.append(Problem(why="decided more than once; decide each belief once" if seen.count(key)
                               else "not decided; decide every belief given", field="decisions", item=key,
                               structural=True))
    return out
