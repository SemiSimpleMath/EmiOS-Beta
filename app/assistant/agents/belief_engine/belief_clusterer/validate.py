"""The belief clusterer's rules (agent_runtime/correction.py). Moved from clusters._settle (2026-11-15). The agent_input
is the page clusters.place_beliefs makes: `beliefs_to_place`, proposed groups of {id, statement}.

Refused: a belief to place in no cluster or in two, and a cluster whose label is blank (the form refuses an empty
one; a label of spaces passes it). An id that is not a belief to place is not refused: it is ignored, as before.
Clusters have no key of their own (two entries with the same label are one topic), so a refused answer is answered
again whole.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    wanted = [b["id"] for group in agent_input["beliefs_to_place"] for b in group]
    out, placed = [], []
    for n, c in enumerate(answer.get("clusters") or []):
        ids = [bid for bid in c.get("belief_ids") or [] if bid in wanted]
        if ids and not str(c.get("label") or "").strip():
            out.append(Problem(why="the cluster has no label; name its topic", field="clusters", at=f"[{n}].label",
                               value=c.get("label")))
        placed += ids
    for bid in wanted:
        if placed.count(bid) != 1:
            out.append(Problem(why="placed more than once; place each belief in exactly one cluster"
                               if placed.count(bid) else "not placed; place every belief to place",
                               field="clusters", value=bid, structural=True))
    return out
