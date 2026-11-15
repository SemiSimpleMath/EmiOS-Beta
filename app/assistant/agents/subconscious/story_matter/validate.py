"""The matter story's rules (agent_runtime/correction.py). Moved from brain_stories.matter_problems (2026-11-15). The
agent_input is brain_stories.matter_input: `events`, each with its E label.

Refused: an empty headline, an empty line, a line for an event not given, more than one line for an event, and an
event with no line.
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"lines": "event"}


def validate(answer, agent_input):
    labels = [e["label"] for e in agent_input["events"]]
    out = [] if str(answer.get("headline") or "").strip() else [Problem(why="the headline is empty", field="headline")]
    given = [str(x.get("event") or "").strip() for x in answer.get("lines") or []]
    for x, event in zip(answer.get("lines") or [], given):
        if event not in labels:
            out.append(Problem(why="it is not one of the event labels given", field="lines", value=event,
                               structural=True))
        if not str(x.get("line") or "").strip():
            out.append(Problem(why="the line is empty", field="lines", item=event, at="line",
                               structural=event not in labels))
    for label in labels:
        if given.count(label) != 1:
            out.append(Problem(why="more than one line for it; one line for each event" if given.count(label)
                               else "no line for it; one line for every event", field="lines", item=label,
                               structural=True))
    return out
