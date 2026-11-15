"""The day's tasks' rules for its entries (agent_runtime/correction.py). Moved from day_tasks.entries_problems
(2026-11-15). The agent_input is day_tasks.gather: `labels` (every label of the call -> [kind, id]) and `changeable`
(day_tasks.changeable: the entries it may modify or cancel, its own O and every open concern's E, each -> the label of
the concern a concern's own entry belongs to, or None).

Refused: a new entry not written as entry "new"; a modify or cancel that names no entry given, or an entry changed
twice; a new entry or a modify without the outcome, the assistant's part and when; an empty why; relies_on naming
nothing or a label not given; a concern that is not an open concern's C label; a concern's own entry moved to another
concern. The action values and the entries list are the form's (agent_form.py). Entries have no key of their own
(every new entry is "new"), so a refused answer is answered again whole.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    entries, labels = agent_input["changeable"], agent_input["labels"]
    out, seen = [], []
    for n, c in enumerate(answer.get("entries") or []):
        at = f"[{n}]"
        entry, action = str(c.get("entry") or "").strip(), c.get("action")
        if action == "new" and entry != "new":
            out.append(Problem(why='a new entry is written as entry "new"', field="entries", at=f"{at}.entry",
                               value=entry))
        if action != "new":
            if entry not in entries:
                out.append(Problem(why="it is not one of the entries given (your own O, a concern's E)",
                                   field="entries", at=f"{at}.entry", value=entry))
                continue
            seen.append(entry)
        if action in ("new", "modify"):
            missing = [f for f in ("what", "assistant_part", "when") if not str(c.get(f) or "").strip()]
            if missing:
                out.append(Problem(why=f"{action} names {', '.join(missing)}", field="entries", at=at))
        if not str(c.get("why") or "").strip():
            out.append(Problem(why="why is empty", field="entries", at=f"{at}.why"))
        relied = [str(x).strip() for x in c.get("relies_on") or []]
        unknown = [x for x in relied if x not in labels]
        if not relied:
            out.append(Problem(why="relies_on names nothing; name what prompted it", field="entries",
                               at=f"{at}.relies_on"))
        elif unknown:
            out.append(Problem(why="these are not labels given", field="entries", at=f"{at}.relies_on",
                               value=unknown))
        concern = str(c.get("concern") or "").strip()
        if concern and (labels.get(concern) or [None])[0] != "concern":
            out.append(Problem(why="it is not an open concern given (its C label)", field="entries",
                               at=f"{at}.concern", value=concern))
        owner = entries.get(entry) if action == "modify" else None
        if owner and concern and concern != owner:
            out.append(Problem(why=f"it is {owner}'s entry and stays with it; leave concern empty or name {owner}",
                               field="entries", at=f"{at}.concern", value=concern))
    for label in sorted({x for x in seen if seen.count(x) > 1}):
        out.append(Problem(why="changed more than once; change each entry once", field="entries", value=label,
                           structural=True))
    return out
