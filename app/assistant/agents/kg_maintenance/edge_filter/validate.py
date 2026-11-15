"""The edge filter's rules (agent_runtime/correction.py). Moved from entity_cards_v2.builder._keep_indices_problems
(2026-11-15). The card builder gives `line_count` (the edges in the batch, numbered 1 to line_count) in agent_input.

Refused: no keep_indices, or an index that is not an edge number in the list. Until 2026-10-05 a failed batch kept
every fact; an empty or stray answer is not a choice of facts either.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    n = agent_input["line_count"]
    keep = answer.get("keep_indices") or []
    if not keep:
        return [Problem(why=f"keep_indices is empty: list the numbers of the edges to keep (1 to {n})",
                        field="keep_indices")]
    stray = [i for i in keep if not isinstance(i, int) or not 1 <= i <= n]
    if stray:
        return [Problem(why=f"these are not edge numbers in the list (1 to {n})", field="keep_indices",
                        value=stray)]
    return []
