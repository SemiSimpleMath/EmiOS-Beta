"""Relations between objects, held by code (docs/design/objects_and_projections_2026-10-08.md).

Owner, 2026-10-08: ids identify objects and are not part of the data; a model's references go in their own fields,
which code turns into relations. One table holds every link: (from kind, from id, rel, to kind, to id, when, by
whom). A relation set is replaced whole when its owner is decided again (an entry's `relies_on` is what its current
decision rests on); the change log of the owner keeps the history of the decision itself.

Which relations exist, and which kinds of object each may join, is the list RELATIONS: a write outside it raises, so
a link is never invented by a typo or a new caller without a decision here. Links are read both ways: what an object
links to (`targets`), and what links to an object (`sources`: when a fact changes, what rests on it).

Users: brain-list entries (`relies_on`, 2026-10-08); the brain's updates and new concerns (`relies_on`, `attach`,
2026-10-08); the architect's tasks and endings (`uses`, `covered_by`, `duplicate_of`, 2026-10-08). A task is
(`task`, its node id); a work object (`work`, its id).
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from app.assistant.subconscious.db import connect as _connect

Ref = Tuple[str, str]

# The kinds of object a relation may name. A brain update is the brain's decision about one concern in one matter
# (`<matter id>:<concern id>`); a brain new concern is one concern the brain raised in a matter (`<matter id>:<n>`).
KINDS: FrozenSet[str] = frozenset({
    "work", "task", "message", "reminder", "concern", "list_entry", "brain_matter", "brain_event", "brain_update",
    "brain_new_concern", "calendar_event", "schedule_line", "resource", "pod", "source", "source_item"})

# rel -> (kinds it may start from, kinds it may point to)
RELATIONS: Dict[str, Tuple[FrozenSet[str], FrozenSet[str]]] = {
    # what a decision rests on: anything it was shown
    "relies_on": (frozenset({"list_entry", "brain_update", "brain_new_concern"}), KINDS),
    # what the brain made part of a concern: an event (kept as the concern's evidence) or a work object
    "attach": (frozenset({"brain_update", "brain_new_concern"}), frozenset({"brain_event", "work"})),
    # what a task must read when it runs (the architect, 2026-10-08): an earlier task's result, a record, other
    # work, the email or message the work came from
    "uses": (frozenset({"task"}), frozenset({"task", "pod", "work", "source_item", "message"})),
    # a goal ended because other work already does or did it (the architect's ending, 2026-10-08)
    "covered_by": (frozenset({"work"}), frozenset({"work"})),
    # a task dropped as a second copy of another task, which is kept
    "duplicate_of": (frozenset({"task"}), frozenset({"task"})),
}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_kind TEXT NOT NULL,
        from_id TEXT NOT NULL,
        rel TEXT NOT NULL,
        to_kind TEXT NOT NULL,
        to_id TEXT NOT NULL,
        at TEXT NOT NULL,
        by TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_relations_from ON relations (from_kind, from_id, rel)",
    "CREATE INDEX IF NOT EXISTS idx_relations_to ON relations (to_kind, to_id, rel)",
]


def ensure_schema(connect=None) -> None:
    with (connect or _connect)(True) as c:
        for statement in SCHEMA:
            c.execute(statement)


def check(frm: Ref, rel: str, targets: Iterable[Ref]) -> None:
    """Raise unless `rel` is a relation in RELATIONS joining these kinds."""
    if rel not in RELATIONS:
        raise ValueError(f"relations: {rel!r} is not a relation (known: {sorted(RELATIONS)})")
    starts, ends = RELATIONS[rel]
    if frm[0] not in starts:
        raise ValueError(f"relations: {rel} does not start from a {frm[0]!r} (it starts from {sorted(starts)})")
    wrong = sorted({kind for kind, _ in targets if kind not in ends})
    if wrong:
        raise ValueError(f"relations: {rel} does not point to {wrong} (it points to {sorted(ends)})")


def replace(c, frm: Ref, rel: str, targets: Iterable[Ref], *, at: str, by: str) -> None:
    """Set `frm`'s `rel` relations to exactly `targets` (in order, without repeats), on the caller's connection."""
    targets = [tuple(t) for t in targets]
    check(frm, rel, targets)
    c.execute("DELETE FROM relations WHERE from_kind = ? AND from_id = ? AND rel = ?", (frm[0], frm[1], rel))
    seen = set()
    for kind, obj_id in targets:
        if (kind, obj_id) in seen:
            continue
        seen.add((kind, obj_id))
        c.execute("INSERT INTO relations (from_kind, from_id, rel, to_kind, to_id, at, by) VALUES (?,?,?,?,?,?,?)",
                  (frm[0], frm[1], rel, kind, obj_id, at, by))


def targets(frm: Ref, rel: str, *, connect=None) -> List[Ref]:
    """`frm`'s current `rel` relations, in the order they were given."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        rows = c.execute("SELECT to_kind, to_id FROM relations WHERE from_kind = ? AND from_id = ? AND rel = ? "
                         "ORDER BY id", (frm[0], frm[1], rel)).fetchall()
    return [(r["to_kind"], r["to_id"]) for r in rows]


def sources(to: Ref, rel: Optional[str] = None, *, connect=None) -> List[Tuple[Ref, str]]:
    """What links to `to`: ((from kind, from id), rel) for each link, oldest first; only `rel` when given."""
    connect = connect or _connect
    ensure_schema(connect)
    sql = "SELECT from_kind, from_id, rel FROM relations WHERE to_kind = ? AND to_id = ?"
    args: list = [to[0], to[1]]
    if rel is not None:
        sql += " AND rel = ?"
        args.append(rel)
    with connect(False) as c:
        rows = c.execute(sql + " ORDER BY id", args).fetchall()
    return [((r["from_kind"], r["from_id"]), r["rel"]) for r in rows]
