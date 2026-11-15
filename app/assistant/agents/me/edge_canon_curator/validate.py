"""The edge canon curator's rules (agent_runtime/correction.py). Moved from step_edge_canon_curation._check_verdicts
(2026-11-15). The curation step gives `predicates` (the batch's novel predicates) and `canons` (the existing canonical
edge types) in agent_input.

Refused: a verdict for a predicate not in the batch, a verdict that is not variant_of, new_canon or not_yet, a
variant_of whose target is not an existing canonical edge type, and a predicate of the batch with no verdict. Every
batch is judged before anything is applied, so a refused answer stops the sweep with nothing written.
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"verdicts": "predicate"}

VERDICTS = ("variant_of", "new_canon", "not_yet")


def validate(answer, agent_input):
    predicates = set(agent_input["predicates"])
    canons = set(agent_input["canons"])
    out, judged = [], set()
    for v in answer.get("verdicts") or []:
        predicate = (v.get("predicate") or "").strip()
        if predicate not in predicates:
            out.append(Problem(why="it is not one of the predicates given; write each predicate verbatim",
                               field="verdicts", value=v.get("predicate"), structural=True))
            continue
        judged.add(predicate)
        verdict = (v.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            out.append(Problem(why=f"the verdict is one of {', '.join(VERDICTS)}", field="verdicts", item=predicate,
                               at="verdict", value=v.get("verdict")))
        elif verdict == "variant_of" and (v.get("canonical_target") or "").strip() not in canons:
            out.append(Problem(why="a variant_of names an existing canonical edge type as its canonical_target",
                               field="verdicts", item=predicate, at="canonical_target",
                               value=v.get("canonical_target")))
    for predicate in sorted(predicates - judged):
        out.append(Problem(why="no verdict; judge every predicate given", field="verdicts", item=predicate,
                           structural=True))
    return out
