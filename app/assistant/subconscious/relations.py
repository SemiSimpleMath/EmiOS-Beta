"""Relations between objects, held by code (docs/design/objects_and_projections_2026-10-08.md).

Owner, 2026-10-08: ids identify objects and are not part of the data; a model's references go in their own fields,
which code turns into relations. One table holds every link: (from kind, from id, rel, to kind, to id, when, by
whom). A relation set is replaced whole when its owner is decided again (an entry's `relies_on` is what its current
decision rests on); the change log of the owner keeps the history of the decision itself.

First user (2026-10-08): brain-list entries, `relies_on` (what an entry's decision rests on).
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from app.assistant.subconscious.db import connect as _connect

Ref = Tuple[str, str]

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
]


def ensure_schema(connect=None) -> None:
    with (connect or _connect)(True) as c:
        for statement in SCHEMA:
            c.execute(statement)


def replace(c, frm: Ref, rel: str, targets: Iterable[Ref], *, at: str, by: str) -> None:
    """Set `frm`'s `rel` relations to exactly `targets` (in order, without repeats), on the caller's connection."""
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
