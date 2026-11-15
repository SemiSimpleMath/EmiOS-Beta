"""The day record's rules for its answer (agent_runtime/correction.py). Moved from day_record.problems (2026-11-15).
The agent_input is day_record.gather: `labels` (every label of the call -> [kind, id]).

Refused: a change that names neither an event given (its K label) nor "new", a new event without its title and start,
a move without the new start, and a change or milestone whose relies_on names nothing or a label not given; a
milestone without its time and what happened. The state and basis values are the form's (agent_form.py). Changes have
no key of their own (every new event is "new") and milestones none, so a refused answer is answered again whole.
"""
from app.assistant.agent_runtime.correction import Problem


def _shows(refs, labels, field, at):
    refs = [str(x).strip() for x in refs or []]
    out = []
    if not refs:
        out.append(Problem(why="relies_on names nothing; name what shows it", field=field, at=f"{at}.relies_on"))
    unknown = [x for x in refs if x not in labels]
    if unknown:
        out.append(Problem(why="these are not labels given", field=field, at=f"{at}.relies_on", value=unknown))
    return out


def validate(answer, agent_input):
    labels = agent_input["labels"]
    out = []
    for n, ch in enumerate(answer.get("changes") or []):
        at = f"[{n}]"
        event = str(ch.get("event") or "").strip()
        if event != "new" and (labels.get(event) or [None])[0] != "day_event":
            out.append(Problem(why='not an event given (its K label) or "new"', field="changes", at=f"{at}.event",
                               value=event))
        if event == "new" and not (str(ch.get("title") or "").strip() and str(ch.get("start") or "").strip()):
            out.append(Problem(why="a new event names its title and its start", field="changes", at=at))
        if ch.get("state") == "moved" and not str(ch.get("start") or "").strip():
            out.append(Problem(why="moved names the new start", field="changes", at=f"{at}.start"))
        out += _shows(ch.get("relies_on"), labels, "changes", at)
    for n, m in enumerate(answer.get("milestones") or []):
        at = f"[{n}]"
        if not str(m.get("time") or "").strip() or not str(m.get("description") or "").strip():
            out.append(Problem(why="a milestone names its time and what happened", field="milestones", at=at))
        out += _shows(m.get("relies_on"), labels, "milestones", at)
    return out
