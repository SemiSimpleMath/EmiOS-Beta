"""The concern door's rules for its decisions (agent_runtime/correction.py). Moved from concern_door._problems
(2026-11-15). The agent_input is concern_door.build_payload: `candidates` (N labels), `existing` (C labels, each with
its status) and `owner_labels` (the candidates whose source is the owner's own words).

Refused: a candidate label not given, a candidate answered twice or not at all, a new matter that names a same_as,
same_open not naming an open concern, same_closed not naming a closed one or said of a candidate from the owner's own
words (it is live), and same_candidate not naming another candidate decided new. The decision values are the form's
(agent_form.py).
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"decisions": "candidate"}


def validate(answer, agent_input):
    cand_labels = [c["label"] for c in agent_input["candidates"]]
    open_labels = [c["label"] for c in agent_input["existing"] if c["status"] in ("active", "addressing")]
    closed_labels = [c["label"] for c in agent_input["existing"] if c["status"] not in ("active", "addressing")]
    owner_labels = list(agent_input["owner_labels"])
    out, seen, by_label = [], [], {}
    for d in answer.get("decisions") or []:
        label = str(d.get("candidate") or "").strip()
        if label not in cand_labels:
            out.append(Problem(why="it is not one of the candidates given", field="decisions", value=label,
                               structural=True))
            continue
        seen.append(label)
        by_label[label] = d
    for label, d in by_label.items():
        decision, target = d.get("decision"), str(d.get("same_as") or "").strip()
        problem = lambda why, structural=False: Problem(why=why, field="decisions", item=label, at="same_as",  # noqa: E731
                                                        value=target, structural=structural)
        if decision == "new" and target:
            out.append(problem("a new matter names no same_as: leave it empty"))
        elif decision == "same_open" and target not in open_labels:
            out.append(problem("same_open names the label of an open concern given"))
        elif decision == "same_closed" and target not in closed_labels:
            out.append(problem("same_closed names the label of a closed concern given"))
        elif decision == "same_closed" and label in owner_labels:
            out.append(Problem(why="it comes from the owner's own words, so it is live: it cannot be same_closed",
                               field="decisions", item=label, at="decision", value=decision))
        elif decision == "same_candidate":
            if target == label or target not in cand_labels:
                out.append(problem("same_candidate names another candidate given"))
            elif (by_label.get(target) or {}).get("decision") != "new":
                out.append(problem(f"same_candidate names a candidate decided new; {target} is not", structural=True))
    for label in cand_labels:
        if seen.count(label) != 1:
            out.append(Problem(why="answer each candidate once" if seen.count(label)
                               else "not answered; answer every candidate", field="decisions", item=label,
                               structural=True))
    return out
