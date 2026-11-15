"""The enricher's rules (agent_runtime/correction.py). Moved from enrich_extraction._call_meta_data_add (2026-11-15).
The enrich step gives `temp_ids` (the State and Event nodes it asked about) in agent_input.

Refused: a node asked about that the answer leaves out. Enrichment is what takes an extraction out of the pending pool
for good, so a node left out would ship into the graph without its dates or validity (owner, 2026-10-05: any failure
stops the run).
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"Nodes": "temp_id"}


def validate(answer, agent_input):
    answered = {str(n.get("temp_id")) for n in answer.get("Nodes") or [] if n.get("temp_id")}
    return [Problem(why="no metadata for this node; answer every node given", field="Nodes", item=tid,
                    structural=True)
            for tid in agent_input["temp_ids"] if tid not in answered]
