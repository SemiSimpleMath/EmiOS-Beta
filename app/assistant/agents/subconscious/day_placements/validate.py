"""The day placements' rules (agent_runtime/correction.py). Moved from calendar_concerns._problems (2026-11-15). The
agent_input is the payload calendar_concerns.build_payload makes: `lines` (L labels) and `concerns` (P labels).

Refused: a line label or concern label not given, a line in no placement or in two, a concern continued twice, an empty
outcome, a time that is neither a clock time nor 'all day', or a clock time without a clock time it matters until.
Placements have no key of their own, so a refused answer is answered again whole.
"""
from app.assistant.agent_runtime.correction import Problem
from app.assistant.subconscious.day_pass import _clock_hour_minute

_CLOCK = "is not a clock time such as '9:00 PM' or '7:30 AM'"


def validate(answer, agent_input):
    line_labels = [x["label"] for x in agent_input["lines"]]
    concern_labels = [x["label"] for x in agent_input["concerns"]]
    out, used, continued = [], [], []
    for n, p in enumerate(answer.get("placements") or []):
        at = f"[{n}]"
        refs = [str(x).strip() for x in p.get("lines") or []]
        if not refs:
            out.append(Problem(why="name the lines it is made of", field="placements", at=f"{at}.lines"))
        for r in refs:
            if r not in line_labels:
                out.append(Problem(why="it is not one of the lines given", field="placements", at=f"{at}.lines",
                                   value=r))
        used += refs
        cont = str(p.get("continues") or "").strip()
        if cont:
            if cont not in concern_labels:
                out.append(Problem(why="it is not one of today's concerns", field="placements",
                                   at=f"{at}.continues", value=cont))
            continued.append(cont)
        if not str(p.get("outcome") or "").strip():
            out.append(Problem(why="say what it asks for today, as an outcome", field="placements",
                               at=f"{at}.outcome"))
        when, until = str(p.get("time") or "").strip(), str(p.get("until") or "").strip()
        if when.lower() == "all day":
            continue
        if _clock_hour_minute(when) is None:
            out.append(Problem(why=f"the time {_CLOCK}, and not 'all day'", field="placements", at=f"{at}.time",
                               value=when))
        elif _clock_hour_minute(until) is None:
            out.append(Problem(why=f"the time it matters until {_CLOCK}; midnight is written '12:00 AM'",
                               field="placements", at=f"{at}.until", value=until))
    for label in line_labels:
        if used.count(label) != 1:
            out.append(Problem(why="put it in exactly one placement" if used.count(label)
                               else "it is in no placement; every line is in one", field="placements", value=label,
                               structural=True))
    for label in sorted(set(continued)):
        if continued.count(label) > 1:
            out.append(Problem(why=f"continued by {continued.count(label)} placements; at most one continues it",
                               field="placements", value=label, structural=True))
    return out
