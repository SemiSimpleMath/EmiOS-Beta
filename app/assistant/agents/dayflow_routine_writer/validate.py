"""The routine writer's rules for its judgments (agent_runtime/correction.py). Moved from day_pass._problems
(2026-11-15). The day pass gives `belief_keys` (the approved beliefs of this batch) and `related_keys` (the beliefs read
beside them) in agent_input.

Refused: a key that is not an approved belief (a related belief is read beside the one it relates to and is never an
entry of its own: on 2026-10-03 the condition "keeps the bedroom fan running continuously" was placed for all day and
handed over as work), an approved belief judged twice or not at all, a belief that applies with no outcome or no time,
a time that is neither a clock time nor 'all day', or a clock time without a clock time it matters until (2026-11-15:
"midnight", twice, and the brain run failed).
"""
from app.assistant.agent_runtime.correction import Problem
from app.assistant.subconscious.day_pass import _clock_hour_minute

ITEM_KEYS = {"judgments": "belief_key"}

_CLOCK = "is not a clock time such as '9:00 PM' or '7:30 AM'"


def validate(answer, agent_input):
    keys = set(agent_input["belief_keys"])
    related = set(agent_input["related_keys"]) - keys
    judgments = answer.get("judgments") or []
    out = []
    judged = [str(j.get("belief_key") or "") for j in judgments]
    for j in judgments:
        key = str(j.get("belief_key") or "")
        if key not in keys:
            bare = key.removeprefix("belief:")
            why = (f"write the key alone, as {bare!r}" if bare in keys
                   else "it is read beside the approved beliefs, not an entry: leave it out" if key in related
                   else "it is not one of the beliefs given")
            out.append(Problem(why=why, field="judgments", value=key, structural=True))
            continue
        if not j.get("applies_today"):
            continue
        if not str(j.get("outcome") or "").strip():
            out.append(Problem(why="it applies today, so say what it asks for today, as an outcome",
                               field="judgments", item=key, at="outcome"))
        if not j.get("times"):
            out.append(Problem(why="it applies today, so give the time it applies, or 'all day'",
                               field="judgments", item=key, at="times"))
        for i, p in enumerate(j.get("times") or []):
            when, until = str((p or {}).get("time") or "").strip(), str((p or {}).get("until") or "").strip()
            if when.lower() == "all day":
                continue
            if _clock_hour_minute(when) is None:
                out.append(Problem(why=f"the time {_CLOCK}, and not 'all day'", field="judgments", item=key,
                                   at=f"times[{i}].time", value=when))
            elif _clock_hour_minute(until) is None:
                out.append(Problem(why=f"the time it matters until {_CLOCK}; midnight is written '12:00 AM'",
                                   field="judgments", item=key, at=f"times[{i}].until", value=until))
    for key in sorted(keys):
        if judged.count(key) != 1:
            out.append(Problem(why="judge each approved belief once" if judged.count(key)
                               else "no judgment; judge every approved belief", field="judgments", item=key,
                               structural=True))
    return out
