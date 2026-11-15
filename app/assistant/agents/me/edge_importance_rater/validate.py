"""The edge importance rater's rules (agent_runtime/correction.py). Moved from scoring._rate_edges and
regenerate_edge_importance (2026-11-15). Every caller gives `edge_ids` (the edges of the batch) in agent_input.

Refused: an edge of the batch with no rating, or whose rating has no finite number as its score (a score of 0 is a
real 0). A rating under an id not in the batch is ignored by the caller, as before.
"""
import math

from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"ratings": "id"}


def _well_formed(score) -> bool:
    return isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)


def validate(answer, agent_input):
    rated, scores = set(), {}
    for r in answer.get("ratings") or []:
        eid = str(r.get("id") or "").strip()
        scores.setdefault(eid, []).append(r.get("score"))
        if _well_formed(r.get("score")):
            rated.add(eid)
    out = []
    for eid in agent_input["edge_ids"]:
        if eid in rated:
            continue
        if eid in scores:
            out.append(Problem(why="the score is a number from 0 to 10", field="ratings", item=eid, at="score",
                               value=scores[eid][-1]))
        else:
            out.append(Problem(why="no rating; rate every edge given, under its id verbatim", field="ratings",
                               item=eid, structural=True))
    return out
