"""The brain's rules for its answer on one matter (agent_runtime/correction.py). Moved from brain_step._problems
(2026-11-15). brain_step.build_payload gives `event_labels` (E…), `concern_labels` (C…), `label_kinds` (every label of
the call -> the kind of object it names) and `now_utc` (the matter's clock, for expiry times) in agent_input.

The contract (docs/design/concerns_as_state_2026-10-09.md, rule 3): every event decided once; an event that changes a
concern names exactly one, which an update records, and an update names only events decided to change it; a new
concern comes from the events decided as its own; references name labels given, and attach names events only; expiry
times are future local times. Only the reference fields are checked, never the words (owner, 2026-10-09). The decision
and action values are the form's (agent_form.py). A problem that spans the decisions and the updates or new concerns
is structural: the answer is corrected whole.
"""
from datetime import datetime

from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"event_decisions": "event", "concern_updates": "concern", "new_concerns": "label"}

# What `attach` may name: an event (its message, email, frame or work report), kept as the concern's evidence.
_ATTACHABLE = ("brain_event",)


def _refs(things, kinds, field, item, at, *, attach):
    """Problems with one reference field: every label given in the call; `attach` names events only."""
    out = []
    names = [str(x).strip() for x in things or []]
    unknown = [x for x in names if x not in kinds]
    if unknown:
        out.append(Problem(why="these are not labels given", field=field, item=item, at=at, value=unknown))
    if attach:
        wrong = [x for x in names if x in kinds and kinds[x] not in _ATTACHABLE]
        if wrong:
            out.append(Problem(why="attach names events (E) only", field=field, item=item, at=at, value=wrong))
    return out


def _when(value, now, field, item):
    """A problem when `value` is not a future local 'YYYY-MM-DD HH:MM'."""
    from app.assistant.subconscious.concern_view import future_local_utc
    try:
        future_local_utc(value, now)
    except ValueError as exc:
        return [Problem(why=f"expires_at must be a future local YYYY-MM-DD HH:MM ({exc})", field=field, item=item,
                        at="expires_at", value=value)]
    return []


def validate(answer, agent_input):
    events = list(agent_input["event_labels"])
    concern_labels = list(agent_input["concern_labels"])
    kinds = dict(agent_input["label_kinds"])
    now = datetime.fromisoformat(agent_input["now_utc"])
    out = []
    decided = {}
    for d in answer.get("event_decisions") or []:
        ev = str(d.get("event") or "").strip()
        if ev not in events:
            out.append(Problem(why="it is not one of the events given", field="event_decisions", value=ev,
                               structural=True))
        elif ev in decided:
            out.append(Problem(why="decided more than once; decide each event once", field="event_decisions",
                               item=ev, structural=True))
        else:
            decided[ev] = (d.get("decision"), str(d.get("concern") or "").strip())
    for ev in events:
        if ev not in decided:
            out.append(Problem(why="not decided; decide every event", field="event_decisions", item=ev,
                               structural=True))

    new_labels = [str(n.get("label") or "").strip() for n in answer.get("new_concerns") or []]
    for ev, (decision, concern) in decided.items():
        if decision == "changes_concern" and concern not in concern_labels:
            out.append(Problem(why="changes_concern names the one open concern given that it changes",
                               field="event_decisions", item=ev, at="concern", value=concern))
        elif decision == "new_concern" and concern not in new_labels:
            out.append(Problem(why="new_concern names one of the new concerns you raise", field="event_decisions",
                               item=ev, at="concern", value=concern, structural=True))
        elif decision == "no_concern" and concern:
            out.append(Problem(why="no_concern names no concern: leave it empty", field="event_decisions", item=ev,
                               at="concern", value=concern))
    changing = lambda label: {ev for ev, (d, c) in decided.items() if d == "changes_concern" and c == label}
    raising = lambda label: {ev for ev, (d, c) in decided.items() if d == "new_concern" and c == label}

    updated = set()
    for u in answer.get("concern_updates") or []:
        label = str(u.get("concern") or "").strip()
        if label not in concern_labels:
            out.append(Problem(why="it is not one of the concerns given", field="concern_updates", value=label,
                               structural=True))
            continue
        if label in updated:
            out.append(Problem(why="updated more than once; update each concern once", field="concern_updates",
                               item=label, structural=True))
        updated.add(label)
        if not str(u.get("note") or "").strip():
            out.append(Problem(why="the note is empty", field="concern_updates", item=label, at="note"))
        relied, attached = u.get("relies_on") or [], u.get("attach") or []
        out += _refs(relied, kinds, "concern_updates", label, "relies_on", attach=False)
        out += _refs(attached, kinds, "concern_updates", label, "attach", attach=True)
        mine = changing(label)
        if not mine:
            out.append(Problem(why="it is updated but no event is decided changes_concern for it: decide the events "
                                   "that change it, or leave it alone", field="concern_updates", item=label,
                               structural=True))
        stray = [x for x in [*relied, *attached] if x in events and x not in mine]
        if stray:
            out.append(Problem(why="these events are not decided to change it; an event changes one concern",
                               field="concern_updates", item=label, value=stray, structural=True))
        if u.get("expires_at"):
            out += _when(u["expires_at"], now, "concern_updates", label)
    for ev, (decision, concern) in decided.items():
        if decision == "changes_concern" and concern in concern_labels and concern not in updated:
            out.append(Problem(why=f"it changes {concern}, but no update of {concern} records it",
                               field="event_decisions", item=ev, structural=True))

    seen_new = set()
    for n in answer.get("new_concerns") or []:
        label = str(n.get("label") or "").strip()
        if not label or label in seen_new:
            out.append(Problem(why="a new concern's label is missing or repeated", field="new_concerns", value=label,
                               structural=True))
        seen_new.add(label)
        if not str(n.get("title") or "").strip() or not str(n.get("done_when") or "").strip():
            out.append(Problem(why="a new concern needs a title and done_when", field="new_concerns", item=label))
        out += _when(n.get("expires_at"), now, "new_concerns", label)
        relied, attached = n.get("relies_on") or [], n.get("attach") or []
        out += _refs(relied, kinds, "new_concerns", label, "relies_on", attach=False)
        out += _refs(attached, kinds, "new_concerns", label, "attach", attach=True)
        mine = raising(label)
        if not mine:
            out.append(Problem(why="no event is decided new_concern for it", field="new_concerns", item=label,
                               structural=True))
        if not any(x in mine for x in attached):
            out.append(Problem(why="attach names none of the events it comes from", field="new_concerns", item=label,
                               at="attach", value=list(attached), structural=True))
        stray = [x for x in attached if x in events and x not in mine]
        if stray:
            out.append(Problem(why="it attaches events that are not decided as its own", field="new_concerns",
                               item=label, at="attach", value=stray, structural=True))
    return out
