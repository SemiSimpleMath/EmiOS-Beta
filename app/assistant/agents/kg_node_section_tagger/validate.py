"""The section tagger's rules (agent_runtime/correction.py). Moved from section_tagging.tag_nodes_by_id (2026-11-15).
The tagger gets `numbers` (the batch numbers of the nodes it is shown) in agent_input.

Refused: a node of the batch with no result. A node left out is never marked processed, so it would be sent again on
every run; a result under a number not in the batch is ignored by the caller, as before.
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"results": "number"}


def validate(answer, agent_input):
    answered = {r.get("number") for r in answer.get("results") or []}
    return [Problem(why="no result for this node; return one TaggedNode for every number given", field="results",
                    item=str(n), structural=True)
            for n in agent_input["numbers"] if n not in answered]
