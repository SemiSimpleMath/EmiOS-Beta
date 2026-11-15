"""The duplicate triage's rules (agent_runtime/correction.py). Moved from step_duplicate_scan._triage_batch
(2026-11-15). The scan gives `pair_count` (the number of pairs in the batch, numbered 1 to pair_count) in agent_input.

Refused: a pair_index outside the batch, a verdict that is not one of the three, two different verdicts for one pair,
or a pair with no verdict. A pair without a verdict would neither be dropped nor reach the detector.
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"pairs": "pair_index"}

VERDICTS = {"distinct", "uncertain", "defective_sentences"}


def validate(answer, agent_input):
    n = agent_input["pair_count"]
    out, answered, verdicts = [], set(), {}
    for v in answer.get("pairs") or []:
        idx = v.get("pair_index")
        if not isinstance(idx, int) or not 1 <= idx <= n:
            out.append(Problem(why=f"it is not one of the pair_index values 1-{n}", field="pairs", value=idx,
                               structural=True))
            continue
        answered.add(idx)
        verdict = str(v.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            out.append(Problem(why=f"the verdict is not one of {sorted(VERDICTS)}", field="pairs", item=str(idx),
                               at="verdict", value=v.get("verdict")))
            continue
        if idx in verdicts and verdicts[idx] != verdict:
            out.append(Problem(why="it has conflicting verdicts; give one verdict per pair", field="pairs",
                               item=str(idx), structural=True))
            continue
        verdicts[idx] = verdict
    for i in range(1, n + 1):
        if i not in answered:
            out.append(Problem(why="no verdict; give one verdict for every pair_index", field="pairs", item=str(i),
                               structural=True))
    return out
