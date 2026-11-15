"""The belief extractor's rules (agent_runtime/correction.py). Moved from intake.agents._extraction_problems
(2026-11-15). The agent_input is the day's payload intake.agents.extract_day makes: `insights`, each with its number
`n` and its `sources`, each with its number `n`.

Refused: a belief citing an insight not given, citing an entry that is not among that insight's own sources, or citing
the same entry twice. The form refuses a belief citing no entry. Beliefs have no key of their own (one insight can give
several), so a refused answer is answered again whole.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    sources_of = {i["n"]: {s["n"] for s in i["sources"]} for i in agent_input["insights"]}
    out = []
    for n, b in enumerate(answer.get("beliefs") or []):
        allowed = sources_of.get(b["insight_ref"])
        if allowed is None:
            out.append(Problem(why=f"it is not one of the insights given, {sorted(sources_of)}", field="beliefs",
                               at=f"[{n}].insight_ref", value=b["insight_ref"]))
            continue
        refs = b.get("sources") or []
        bad = [r for r in refs if r not in allowed]
        if bad:
            out.append(Problem(why=f"entries {bad} are not among insight {b['insight_ref']}'s sources "
                               f"{sorted(allowed)}; cite only its own sources", field="beliefs", at=f"[{n}].sources",
                               value=refs))
        if len(set(refs)) != len(refs):
            out.append(Problem(why="it cites the same entry twice; cite each entry once", field="beliefs",
                               at=f"[{n}].sources", value=refs))
    return out
