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
            # calendar arrived 2026-10-03: whether the belief is day-planning material. NULL until the nightly
            # tagger looks at it (intake/calendar.py), then proposed | not_proposed; the owner's decision is
            # approved | declined. calendar_note: when it would appear on the calendar, and why.
            if "calendar" not in cols:
                c.execute(f"ALTER TABLE {prefix}beliefs ADD COLUMN calendar TEXT")
                c.execute(f"ALTER TABLE {prefix}beliefs ADD COLUMN calendar_note TEXT")
            # review arrived 2026-10-03: the owner's morning review of a new belief: NULL until reviewed, then
            # approved | dismissed | edited, on reviewed_day. Beliefs that existed when the column arrived are
            # `baseline`: they predate the review (the owner cleaned the catalog by hand that day).
            if "review" not in cols:
                c.execute(f"ALTER TABLE {prefix}beliefs ADD COLUMN review TEXT")
                c.execute(f"ALTER TABLE {prefix}beliefs ADD COLUMN reviewed_day TEXT")
                c.execute(f"UPDATE {prefix}beliefs SET review='baseline'")
            # contradicts arrived 2026-10-03: a new belief that contradicts held ones is kept `pending` for the
            # owner with the ids it contradicts (a JSON list); nothing is revised until the owner decides.
            if "contradicts" not in cols:
                c.execute(f"ALTER TABLE {prefix}beliefs ADD COLUMN contradicts TEXT")
            # drafts: for a held contradiction, the drafted rewrite of each belief it contradicts that reconciles
            # them (a JSON object id -> {statement, kind, reasoning}), applied only on the owner's word.
            if "drafts" not in cols:
                c.execute(f"ALTER TABLE {prefix}beliefs ADD COLUMN drafts TEXT")

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

    def merge(self, drop_id: str, into_id: str, day: str, reason: str) -> int:
        """Fold a duplicate into the belief that states the same claim: every evidence row of `drop_id` moves to
        `into_id`, and `drop_id` is retired. Both histories record the merge; the duplicate's statement stays in
        its own. Returns the evidence rows moved. Raises unless both beliefs are active and distinct."""
        if drop_id == into_id:
            raise ValueError(f"cannot merge {drop_id} into itself")
        with self._connect(True) as c:
            rows = {r["id"]: r for r in c.execute(
                f"SELECT id, statement, kind, status FROM {self.p}beliefs WHERE id IN (?, ?)", (drop_id, into_id))}
            for bid in (drop_id, into_id):
                if bid not in rows or rows[bid]["status"] != "active":
                    raise ValueError(f"belief {bid} is not active")
            moved = c.execute(f"UPDATE {self.p}evidence SET belief_id=? WHERE belief_id=?", (into_id, drop_id)).rowcount
            for bid, note in ((drop_id, f"retired: merged into {into_id} ({reason})"),
                              (into_id, f"merged {drop_id} into it, {moved} evidence row(s) moved ({reason})")):
                r = rows[bid]
                c.execute(
                    f"INSERT INTO {self.p}revisions (belief_id, day, old_statement, new_statement, old_kind, new_kind, "
                    "reasoning) VALUES (?,?,?,?,?,?,?)", (bid, day, r["statement"], r["statement"], r["kind"], r["kind"], note))
            c.execute(f"UPDATE {self.p}beliefs SET status='retired' WHERE id=?", (drop_id,))
        return moved

    def split(self, belief_id: str, day: str, statements: list[str], embeddings: list[list], reason: str) -> list[str]:
        """Split a belief that holds several claims into one belief per claim. The first statement replaces
        the belief's own (a revision), so whatever cites its id keeps its main claim; each further statement
        becomes a new belief with the same kind, scope and parent, resting on a copy of every evidence row
        (via `split:<id>`: the evidence supported the combined statement, so it supports each part). Returns
        the new ids. Raises unless the belief is active and there are at least two statements."""
        if len(statements) < 2 or len(statements) != len(embeddings):
            raise ValueError("a split needs two or more statements, each with its embedding")
        with self._connect(True) as c:
            row = c.execute(f"SELECT * FROM {self.p}beliefs WHERE id=?", (belief_id,)).fetchone()
            if row is None or row["status"] != "active":
                raise ValueError(f"belief {belief_id} is not active")
            evidence = c.execute(f"SELECT day, time, kind, relation, text, in_reply_to, source_ref FROM "
                                 f"{self.p}evidence WHERE belief_id=? ORDER BY id", (belief_id,)).fetchall()
            n = c.execute(f"SELECT COALESCE(MAX(CAST(SUBSTR(id, 2) AS INTEGER)), 0) FROM {self.p}beliefs").fetchone()[0]
            new_ids = [f"B{n + i}" for i in range(1, len(statements))]
            for bid, statement, embedding in zip(new_ids, statements[1:], embeddings[1:]):
                c.execute(f"INSERT INTO {self.p}beliefs (id, statement, later_use, kind, scope, parent_id, "
                          "created_day, embedding) VALUES (?,?,?,?,?,?,?,?)",
                          (bid, statement, row["later_use"], row["kind"], row["scope"], row["parent_id"], day,
                           json.dumps(embedding)))
                c.executemany(
                    f"INSERT INTO {self.p}evidence (belief_id, day, time, kind, relation, text, in_reply_to, "
                    "source_ref, via) VALUES (?,?,?,?,?,?,?,?,?)",
                    [(bid, e["day"], e["time"], e["kind"], e["relation"], e["text"], e["in_reply_to"],
                      e["source_ref"], f"split:{belief_id}") for e in evidence])
                c.execute(f"INSERT INTO {self.p}revisions (belief_id, day, old_statement, new_statement, old_kind, "
                          "new_kind, reasoning) VALUES (?,?,?,?,?,?,?)",
                          (bid, day, row["statement"], statement, row["kind"], row["kind"],
                           f"split from {belief_id}: {reason}"))
            c.execute(f"INSERT INTO {self.p}revisions (belief_id, day, old_statement, new_statement, old_kind, "
                      "new_kind, reasoning) VALUES (?,?,?,?,?,?,?)",
                      (belief_id, day, row["statement"], statements[0], row["kind"], row["kind"],
                       f"split into {', '.join(new_ids)}: {reason}"))
            c.execute(f"UPDATE {self.p}beliefs SET statement=?, embedding=? WHERE id=?",
                      (statements[0], json.dumps(embeddings[0]), belief_id))
        return new_ids

    def calendar_undecided(self) -> list[dict]:
        """Active beliefs the calendar tagger has not looked at: {id, statement}, in creation order."""
        with self._connect(False) as c:
            return [dict(r) for r in c.execute(
                f"SELECT id, statement FROM {self.p}beliefs WHERE status='active' AND calendar IS NULL ORDER BY rowid")]

    def set_calendar(self, belief_id: str, status: str, note: str | None) -> None:
        """Record whether a belief is day-planning material: the tagger's proposed | not_proposed, or the
        owner's approved | declined."""
        if status not in ("proposed", "not_proposed", "approved", "declined"):
            raise ValueError(f"unknown calendar status {status!r}")
        with self._connect(True) as c:
            c.execute(f"UPDATE {self.p}beliefs SET calendar=?, calendar_note=? WHERE id=?", (status, note, belief_id))

    def hold(self, day: str, atom: dict, embedding: list, targets: list[str], reasoning: str | None,
             drafts: dict | None = None) -> str:
        """A new belief that contradicts held ones, kept `pending` for the owner: its own sources as support,
        the ids it contradicts in `contradicts`, and for each of them a drafted rewrite that reconciles the two
        (`drafts`, id -> {statement, kind, reasoning}; none when no rewrite reconciles them). The held beliefs are
        left as they are. Returns its id."""
        if not targets:
            raise ValueError("a held contradiction names the beliefs it contradicts")
        with self._connect(True) as c:
            n = c.execute(f"SELECT COALESCE(MAX(CAST(SUBSTR(id, 2) AS INTEGER)), 0) FROM {self.p}beliefs").fetchone()[0]
            bid = f"B{n + 1}"
            c.execute(f"INSERT INTO {self.p}beliefs (id, statement, later_use, kind, scope, parent_id, created_day, "
                      "embedding, status, contradicts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (bid, atom["statement"], atom.get("later_use"), atom["kind"], atom.get("scope"), None, day,
                       json.dumps(embedding), "pending", json.dumps(targets)))
            c.execute(f"UPDATE {self.p}beliefs SET drafts=? WHERE id=?", (json.dumps(drafts or {}), bid))
            for s in atom["sources"]:
                c.execute(
                    f"INSERT INTO {self.p}evidence (belief_id, day, time, kind, relation, text, in_reply_to, source_ref, via) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (bid, day, s.get("time"), s["kind"], s["relation"], s.get("text"), s.get("in_reply_to"),
                     s.get("source_ref"), "contradicts"))
            c.execute(f"INSERT INTO {self.p}decisions (day, statement, verdict, target, result_belief, reasoning) "
                      "VALUES (?,?,?,?,?,?)", (day, atom["statement"], "contradicts", targets[0], bid, reasoning))
        return bid

    def resolve_contradiction(self, belief_id: str, keep: str, day: str,
                              rewrites: dict[str, tuple[str, list]] | None = None) -> None:
        """The owner's decision on a held contradiction. new: the new belief becomes active and the beliefs it
        contradicts are retired; old: the new belief is retired; both: the new belief becomes active beside them;
        rewrite: each belief in `rewrites` (id -> (statement, embedding): the drafted rewrite, or the owner's edit
        of it) takes that wording, gains the new belief's evidence as support, and the new belief is retired,
        folded into them."""
        if keep not in ("new", "old", "both", "rewrite"):
            raise ValueError(f"unknown decision {keep!r}")
        with self._connect(False) as c:
            row = c.execute(f"SELECT status, contradicts, kind FROM {self.p}beliefs WHERE id=?", (belief_id,)).fetchone()
        if row is None or row["status"] != "pending":
            raise ValueError(f"belief {belief_id} is not a held contradiction")
        targets = json.loads(row["contradicts"])
        if keep == "rewrite":
            if not rewrites or not set(rewrites) <= set(targets):
                raise ValueError(f"a rewrite names beliefs {sorted(rewrites or {})} the contradiction does not: {targets}")
            with self._connect(True) as c:
                evidence = c.execute(f"SELECT day, time, kind, relation, text, in_reply_to, source_ref FROM "
                                     f"{self.p}evidence WHERE belief_id=? ORDER BY id", (belief_id,)).fetchall()
                for t in rewrites:
                    c.executemany(
                        f"INSERT INTO {self.p}evidence (belief_id, day, time, kind, relation, text, in_reply_to, "
                        "source_ref, via) VALUES (?,?,?,?,?,?,?,?,?)",
                        [(t, e["day"], e["time"], e["kind"], "support", e["text"], e["in_reply_to"], e["source_ref"],
                          f"reconciled:{belief_id}") for e in evidence])
            for t, (statement, embedding) in rewrites.items():
                kind = (self.get(t) or {}).get("kind")
                self.revise(t, day, {"statement": statement, "kind": kind,
                                     "reasoning": f"the owner applied the rewrite reconciling it with {belief_id}"},
                            embedding)
            self.retire(belief_id, day, f"folded into {', '.join(rewrites)} by the owner's rewrite")
            self.set_review(belief_id, "approved", day)
            return
        if keep == "old":
            self.retire(belief_id, day, f"the owner kept {', '.join(targets)} over it")
            self.set_review(belief_id, "dismissed", day)
            return
        with self._connect(True) as c:
            c.execute(f"UPDATE {self.p}beliefs SET status='active' WHERE id=?", (belief_id,))
        self.set_review(belief_id, "approved", day)
        if keep == "new":
            for t in targets:
                if (self.get(t) or {}).get("status") == "active":
                    self.retire(t, day, f"replaced by {belief_id} on the owner's review")

    def set_review(self, belief_id: str, review: str, day: str) -> None:
        """Record the owner's review of a belief: approved | dismissed | edited."""
        if review not in ("approved", "dismissed", "edited"):
            raise ValueError(f"unknown review {review!r}")
        with self._connect(True) as c:
            c.execute(f"UPDATE {self.p}beliefs SET review=?, reviewed_day=? WHERE id=?", (review, day, belief_id))

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
