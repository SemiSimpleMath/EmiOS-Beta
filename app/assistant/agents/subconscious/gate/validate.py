"""The gate's rules for its routing (agent_runtime/correction.py). Moved from gate._problems (2026-11-15). The gate
gives `event_labels` (E1… for the page's events) and `open_concerns` (each with its C label) in agent_input.

Refused: an event label not given, an event answered twice or not at all, a concern route that does not name one open
concern given, and another route that names a concern (docs/design/concerns_as_state_2026-10-09.md, rule 3: one event
changes one concern). That the answer is a decisions list and each route is concern, new_matter or none is the
form's (agent_form.py).
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"decisions": "event"}


def validate(answer, agent_input):
    event_labels = list(agent_input["event_labels"])
    concern_labels = [c["label"] for c in agent_input["open_concerns"]]
    out, seen = [], []
    for d in answer.get("decisions") or []:
        ev = str(d.get("event") or "").strip()
        if ev not in event_labels:
            out.append(Problem(why="it is not one of the events given", field="decisions", value=ev, structural=True))
            continue
        seen.append(ev)
        route, named = d.get("route"), str(d.get("concern") or "").strip()
        if route == "concern" and named not in concern_labels:
            out.append(Problem(why="route concern names the label of the one open concern it changes, one of those "
                                   "given", field="decisions", item=ev, at="concern", value=named))
        elif route != "concern" and named:
            out.append(Problem(why=f"route {route} names no concern: leave it empty", field="decisions", item=ev,
                               at="concern", value=named))
    for ev in event_labels:
        if seen.count(ev) != 1:
            out.append(Problem(why="answer each event once" if seen.count(ev)
                               else "not answered; answer every event", field="decisions", item=ev, structural=True))
    return out
