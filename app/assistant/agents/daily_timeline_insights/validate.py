"""The insights writer's rules (agent_runtime/correction.py). Moved from archive_daily_insights.cite_sources
(2026-11-15). The archive step gives `entry_numbers` (the numbers `n` of the day's entries) in agent_input.

Refused: an insight citing no entries, or citing a number that is no entry of the day: its citations become its stored
provenance (owner, 2026-10-03: "Everything needs to have provenance"). Insights have no key of their own, so a refused
answer is answered again whole.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    numbers = set(agent_input["entry_numbers"])
    out = []
    for i, item in enumerate(answer.get("actionable_information") or []):
        cited = list(dict.fromkeys(item.get("sources") or []))
        if not cited:
            out.append(Problem(why="it cites no entries; give the numbers `n` of the entries it rests on",
                               field="actionable_information", at=f"[{i}].sources"))
        bad = [n for n in cited if n not in numbers]
        if bad:
            out.append(Problem(why="not entry numbers of the day; cite only the numbers `n` of the timeline entries",
                               field="actionable_information", at=f"[{i}].sources", value=bad))
    return out
