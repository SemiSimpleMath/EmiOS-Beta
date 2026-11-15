"""The belief dedup's rules (agent_runtime/correction.py). Moved from intake.agents.dedup (2026-11-15). The
agent_input is the payload dedup makes: `new_belief` with its numbered `sources`, and `existing_beliefs`, each with its
`id`.

Refused: same, refines or contradicts with a target that is not one of the listed ids; same or contradicts naming no
bearing source, or a source number outside the new belief's sources. The form refuses a verdict that is not one of
same, refines, contradicts, new.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    ids = [b["id"] for b in agent_input["existing_beliefs"]]
    count = len(agent_input["new_belief"]["sources"])
    v, target, sources = answer["verdict"], (answer.get("target") or "").strip(), answer.get("sources") or []
    out = []
    if v != "new" and target not in ids:
        out.append(Problem(why=f"it is not one of the listed ids {ids}; copy the id exactly", field="target",
                           value=answer.get("target")))
    if v in ("same", "contradicts"):
        if not sources:
            out.append(Problem(why=f"{v} names no bearing sources; give the numbers of the new belief's sources that "
                               f"bear on the target", field="sources"))
        bad = [n for n in sources if not 1 <= n <= count]
        if bad:
            out.append(Problem(why=f"sources {bad} are outside 1..{count}", field="sources", value=sources))
    return out
