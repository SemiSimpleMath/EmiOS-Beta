"""The duplicate cluster resolver's coverage contract (agent_runtime/correction.py). Moved from
duplicate_cluster_drain._coverage_problems (2026-11-15). The drain gives `node_ids` (the cluster's node ids) in
agent_input.

Refused: a merge group that names no duplicate, an id that is not one of the cluster's, a node placed more than once,
or a node not placed at all. A node the answer leaves out is never taken as 'distinct': applying such an answer would
record its pairs as distinct, a verdict nobody made.
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"merge_groups": "canonical_node_id"}


def validate(answer, agent_input):
    node_ids = list(agent_input["node_ids"])
    node_set = set(node_ids)
    named, out = [], []
    for g in answer.get("merge_groups") or []:
        canon = g.get("canonical_node_id")
        dups = list(g.get("duplicate_node_ids") or [])
        if not dups:
            out.append(Problem(why="the merge group names no duplicate_node_ids; a merge group names at least one",
                               field="merge_groups", item=str(canon), at="duplicate_node_ids"))
        named.append(canon)
        named.extend(dups)
    named.extend(answer.get("distinct_node_ids") or [])
    for n in sorted({str(n) for n in named if n not in node_set}):
        out.append(Problem(why="it is not an id of this cluster; use only the ids shown", value=n, structural=True))
    for n in sorted({n for n in named if n in node_set and named.count(n) > 1}):
        out.append(Problem(why="placed more than once; place each node exactly once", value=n, structural=True))
    for n in node_ids:
        if n not in named:
            out.append(Problem(why="not placed; place every node exactly once, in a merge group or in "
                                   "distinct_node_ids", value=n, structural=True))
    return out
