"""The goal outcome detector's rules (agent_runtime/correction.py). Moved from step_goal_outcome_detect._ask
(2026-11-15). The step gives `goal_node_ids` (the candidate Goals' ids) in agent_input.

Refused: a goal_node_id that is not a candidate, an outcome that is not one of the three, a confidence that is not a
number, or a candidate with no verdict.
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"verdicts": "goal_node_id"}

OUTCOMES = {"completed", "abandoned", "no_signal"}


def validate(answer, agent_input):
    ids = list(agent_input["goal_node_ids"])
    out, answered = [], set()
    for v in answer.get("verdicts") or []:
        gid = v.get("goal_node_id")
        if gid not in ids:
            out.append(Problem(why="it is not one of the candidate goal_node_ids", field="verdicts", value=gid,
                               structural=True))
            continue
        answered.add(gid)
        outcome = str(v.get("outcome") or "").strip().lower()
        if outcome not in OUTCOMES:
            out.append(Problem(why=f"the outcome is not one of {sorted(OUTCOMES)}", field="verdicts", item=gid,
                               at="outcome", value=v.get("outcome")))
            continue
        try:
            float(v.get("confidence"))
        except (TypeError, ValueError):
            out.append(Problem(why="the confidence is not a number", field="verdicts", item=gid, at="confidence",
                               value=v.get("confidence")))
    for gid in ids:
        if gid not in answered:
            out.append(Problem(why="no verdict; give one verdict for every candidate goal_node_id", field="verdicts",
                               item=gid, structural=True))
    return out
