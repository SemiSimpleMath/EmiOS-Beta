"""The intake's own tables: beliefs as a view of their evidence, every decision and revision kept.

One schema, two homes. In the app DB (shadow mode) the tables are prefixed `belief_intake_` and
every write goes through db_manager in one short transaction; in a scratch sqlite file (replays)
they are unprefixed. Beliefs are read before a model call and written after it — no connection is
held across a model call.

Every evidence row points at the exact timeline item it came from (source_ref = message:<id> |
ticket:<id>). Parent/child is one level; `parent_id` is the link the owner approved on 2026-09-27.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, ContextManager, Iterator

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS {p}beliefs (
         id TEXT PRIMARY KEY, statement TEXT NOT NULL, later_use TEXT, kind TEXT NOT NULL, scope TEXT,
         parent_id TEXT REFERENCES {p}beliefs(id), created_day TEXT NOT NULL, embedding TEXT NOT NULL,
         status TEXT NOT NULL DEFAULT 'active')""",
    """CREATE TABLE IF NOT EXISTS {p}evidence (
         id INTEGER PRIMARY KEY AUTOINCREMENT, belief_id TEXT NOT NULL REFERENCES {p}beliefs(id),
         day TEXT NOT NULL, time TEXT, kind TEXT NOT NULL, relation TEXT NOT NULL, text TEXT,
         in_reply_to TEXT, source_ref TEXT, via TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS {p}evidence_belief ON {p}evidence(belief_id)""",
    """CREATE TABLE IF NOT EXISTS {p}decisions (
         id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, statement TEXT NOT NULL,
         verdict TEXT NOT NULL, target TEXT, result_belief TEXT, reasoning TEXT)""",
    """CREATE TABLE IF NOT EXISTS {p}revisions (
         id INTEGER PRIMARY KEY AUTOINCREMENT, belief_id TEXT NOT NULL, day TEXT NOT NULL,
         old_statement TEXT NOT NULL, new_statement TEXT NOT NULL, old_kind TEXT, new_kind TEXT, reasoning TEXT)""",
    """CREATE TABLE IF NOT EXISTS {p}days (
         day TEXT PRIMARY KEY, status TEXT NOT NULL, atoms INTEGER, note TEXT)""",
]

Connect = Callable[[bool], ContextManager[sqlite3.Connection]]


def sqlite_file(path: Path) -> Connect:
    """A scratch store in its own file (replays)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    @contextmanager
    def connect(write: bool) -> Iterator[sqlite3.Connection]:
        if write:
            with conn:
                yield conn
        else:
            yield conn
    return connect


def app_db() -> Connect:
    """The app DB: writes hold db_manager's writer slot for the transaction only; reads take none."""
    from app.models.db_manager import get_db_manager

    @contextmanager
    def connect(write: bool) -> Iterator[sqlite3.Connection]:
        mgr = get_db_manager()
        with (mgr.transaction(op="belief_intake") if write else mgr.read_session()) as session:
            raw = session.connection().connection.driver_connection
            previous = raw.row_factory
            raw.row_factory = sqlite3.Row
            try:
                yield raw
            finally:
                raw.row_factory = previous
    return connect


class IntakeStore:
    def __init__(self, connect: Connect, prefix: str = ""):
        self._connect = connect
        self.p = prefix
        with self._connect(True) as c:
            for statement in SCHEMA:
                c.execute(statement.format(p=prefix))
            # status arrived with the 2026-09-29 cutover (a work outcome can retire a belief); tables
            # created before it gain the column with every existing belief active.
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({prefix}beliefs)")}
            if "status" not in cols:
                c.execute(f"ALTER TABLE {prefix}beliefs ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")

    def days_done(self) -> set[str]:
        with self._connect(False) as c:
            return {r[0] for r in c.execute(f"SELECT day FROM {self.p}days WHERE status='done'")}

    def beliefs(self, *, include_retired: bool = False) -> list[dict]:
        """Every active belief (or every belief) with its evidence, in creation order."""
        where = "" if include_retired else " WHERE status='active'"
        with self._connect(False) as c:
            rows = [dict(r) for r in c.execute(f"SELECT * FROM {self.p}beliefs{where} ORDER BY rowid")]
            evidence = [dict(r) for r in c.execute(
                f"SELECT belief_id, day, time, kind, relation, text, in_reply_to FROM {self.p}evidence ORDER BY id")]
        by_belief: dict[str, list] = {}
        for e in evidence:
            by_belief.setdefault(e.pop("belief_id"), []).append(e)
        for b in rows:
            b["embedding"] = json.loads(b["embedding"])
            b["sources"] = by_belief.get(b["id"], [])
        return rows

    def apply(self, day: str, atom: dict, embedding: list, verdict: dict) -> str:
        """Apply one checked atom in one transaction. Returns the id of the belief it landed on.

        A new or refining belief keeps every source. On same/contradicts the existing belief gains
        only the sources dedup named as bearing on it (`verdict["sources"]`, 1-based); a
        multi-claim message's other sources stay with the claims they support.
        """
        v, target = verdict["verdict"], verdict.get("target")
        sources = atom["sources"]
        if v in ("same", "contradicts"):
            sources = [sources[n - 1] for n in verdict["sources"]]
        with self._connect(True) as c:
            if v in ("new", "refines"):
                parent = None
                if v == "refines":
                    row = c.execute(f"SELECT parent_id FROM {self.p}beliefs WHERE id=?", (target,)).fetchone()
                    parent = row["parent_id"] or target          # one level only
                # Highest number + 1, never the row count: ids are cross-system keys (routine
                # citations, work-object belief_refs) and a count reuses the id of any removed row.
                n = c.execute(f"SELECT COALESCE(MAX(CAST(SUBSTR(id, 2) AS INTEGER)), 0) FROM {self.p}beliefs").fetchone()[0]
                bid = f"B{n + 1}"
                c.execute(f"INSERT INTO {self.p}beliefs (id, statement, later_use, kind, scope, parent_id, "
                          "created_day, embedding) VALUES (?,?,?,?,?,?,?,?)",
                          (bid, atom["statement"], atom.get("later_use"), atom["kind"], atom.get("scope"), parent, day,
                           json.dumps(embedding)))
            else:
                bid = target
            relation_override = "contradict" if v == "contradicts" else None
            for s in sources:
                c.execute(
                    f"INSERT INTO {self.p}evidence (belief_id, day, time, kind, relation, text, in_reply_to, source_ref, via) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (bid, day, s.get("time"), s["kind"], relation_override or s["relation"], s.get("text"),
                     s.get("in_reply_to"), s.get("source_ref"), v))
            c.execute(
                f"INSERT INTO {self.p}decisions (day, statement, verdict, target, result_belief, reasoning) VALUES (?,?,?,?,?,?)",
                (day, atom["statement"], v, target, bid, verdict.get("reasoning")))
        return bid

    def revise(self, belief_id: str, day: str, revision: dict, embedding: list) -> None:
        """Rewrite a belief's statement, keeping the old one in the revision history."""
        with self._connect(True) as c:
            row = c.execute(f"SELECT statement, kind FROM {self.p}beliefs WHERE id=?", (belief_id,)).fetchone()
            c.execute(
                f"INSERT INTO {self.p}revisions (belief_id, day, old_statement, new_statement, old_kind, new_kind, reasoning) "
                "VALUES (?,?,?,?,?,?,?)",
                (belief_id, day, row["statement"], revision["statement"], row["kind"], revision["kind"],
                 revision.get("reasoning")))
            c.execute(f"UPDATE {self.p}beliefs SET statement=?, kind=?, embedding=? WHERE id=?",
                      (revision["statement"], revision["kind"], json.dumps(embedding), belief_id))

    def get(self, belief_id: str) -> dict | None:
        """One belief, active or retired, with its last supporting day; None when the id is unknown."""
        with self._connect(False) as c:
            row = c.execute(f"SELECT id, statement, kind, scope, status FROM {self.p}beliefs WHERE id=?",
                            (belief_id,)).fetchone()
            if row is None:
                return None
            last = c.execute(f"SELECT MAX(day) FROM {self.p}evidence WHERE belief_id=? AND relation='support'",
                             (belief_id,)).fetchone()[0]
        return {**dict(row), "last_confirmed": last}

    def add_evidence(self, belief_id: str, day: str, *, kind: str, relation: str, text: str,
                     source_ref: str, via: str, time: str | None = None) -> None:
        """One evidence row from outside the day intake (a work outcome, a comment pod)."""
        with self._connect(True) as c:
            c.execute(
                f"INSERT INTO {self.p}evidence (belief_id, day, time, kind, relation, text, in_reply_to, source_ref, via) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (belief_id, day, time, kind, relation, text, None, source_ref, via))

    def has_evidence(self, belief_id: str, source_ref: str) -> bool:
        with self._connect(False) as c:
            return c.execute(f"SELECT 1 FROM {self.p}evidence WHERE belief_id=? AND source_ref=? LIMIT 1",
                             (belief_id, source_ref)).fetchone() is not None

    def retire(self, belief_id: str, day: str, reason: str) -> None:
        """The belief's condition was met (a work outcome resolved it): kept, no longer offered."""
        with self._connect(True) as c:
            row = c.execute(f"SELECT statement, kind FROM {self.p}beliefs WHERE id=?", (belief_id,)).fetchone()
            c.execute(
                f"INSERT INTO {self.p}revisions (belief_id, day, old_statement, new_statement, old_kind, new_kind, reasoning) "
                "VALUES (?,?,?,?,?,?,?)", (belief_id, day, row["statement"], row["statement"], row["kind"], row["kind"],
                                          f"retired: {reason}"))
            c.execute(f"UPDATE {self.p}beliefs SET status='retired' WHERE id=?", (belief_id,))

    def restore(self, belief_id: str, day: str, reason: str) -> None:
        """Undo a retirement (the owner's call in /beliefs); recorded in the revision history."""
        with self._connect(True) as c:
            row = c.execute(f"SELECT statement, kind FROM {self.p}beliefs WHERE id=?", (belief_id,)).fetchone()
            c.execute(
                f"INSERT INTO {self.p}revisions (belief_id, day, old_statement, new_statement, old_kind, new_kind, reasoning) "
                "VALUES (?,?,?,?,?,?,?)", (belief_id, day, row["statement"], row["statement"], row["kind"], row["kind"],
                                          f"restored: {reason}"))
            c.execute(f"UPDATE {self.p}beliefs SET status='active' WHERE id=?", (belief_id,))

    def mark_day(self, day: str, status: str, atoms: int, note: str = "") -> None:
        with self._connect(True) as c:
            c.execute(f"INSERT OR REPLACE INTO {self.p}days VALUES (?,?,?,?)", (day, status, atoms, note))
