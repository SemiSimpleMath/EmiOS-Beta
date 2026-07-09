"""
work_objects.store — own SQLite persistence + the validated writer.

The **event log is the record of truth**; the `nodes`/`edges` tables are the
live **projection** (a materialized cache you read from, rebuildable from events).

Every mutation goes through `WorkStore.apply(op, data, actor)`:
    1. load the current projection
    2. validate the patch  — allowed status transitions + authority ceiling +
       structural invariants (WorkObject.validate)
    3. append the event
    4. update the projection
    5. recompute the derived rollup (WorkObject.status)
  Steps 3-5 are ONE atomic sqlite transaction: the event and the projection
  commit together or not at all — they can never diverge.

Writers serialize on the store's in-process RLock — the dayflow pipeline
nodes, the scheduler's wake threads, the worker, and the /work UI all write
through here — and each apply() is one short atomic transaction.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Callable, Optional

from work_objects.model import (
    WAKE_KINDS, Edge, SCHEMA_SQL, WorkNode, WorkObject, _TERMINAL_STATUSES, utcnow,
)

# --------------------------------------------------------------------------- #
# Status state machine, keyed by node FAMILY (inferred from type). A new node
# type defaults to the "spine" lifecycle until it's mapped here.
# --------------------------------------------------------------------------- #
FAMILY_BY_TYPE = {
    "goal": "spine", "plan": "spine", "subtask": "spine", "tool": "spine",
    "notify": "notify",                          # legacy rows only — kept so old graphs load + transition
    "evidence": "knowledge", "artifact": "knowledge",
    "question": "question", "verification": "verification",
}
TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "spine": {
        # proposed = architect's inbox (born here). state_mover promotes proposed->actionable
        # when the node's gates (time/deps) are clear; only THEN may the action_selector dispatch it.
        # dispatched = in-flight (a worker is on it, or an ask is out) — the action_selector/gate are blind to it.
        # done = work was done (manager's verdict) | incomplete = acted on but couldn't (+ why). Both await the
        # finalizer, which ALONE produces closed (the satisfied terminal). [step 2 keys is_satisfied on closed]
        "proposed": {"actionable", "dispatched", "waiting", "failed", "abandoned"},   # failed = dispatch broke before the node ran
        "actionable": {"dispatched", "waiting", "failed", "abandoned"},   # state_mover-promoted; awaiting action_selector dispatch
        "dispatched": {"waiting", "done", "incomplete", "failed", "abandoned"},
        "waiting": {"actionable", "dispatched", "done", "incomplete", "failed", "abandoned"},
        "done": {"closed", "superseded"},
        "incomplete": {"closed", "actionable", "abandoned"},
        "closed": {"superseded"},
        "failed": {"dispatched", "proposed", "abandoned"},   # proposed = re-open: the work_repair adjudicator re-issues a failed node
        "abandoned": set(), "superseded": set(),
    },
    "notify": {
        # LEGACY family (pre-unification notify rows): those could complete straight from
        # proposed (the send was the completion). New graphs mint plain spine nodes and the
        # switchboard reads their goals; this family remains so existing rows keep loading
        # and transitioning legally.
        "proposed": {"actionable", "dispatched", "waiting", "done", "failed", "abandoned"},
        "actionable": {"dispatched", "waiting", "done", "failed", "abandoned"},   # state_mover-promoted; awaiting action_selector dispatch
        "dispatched": {"waiting", "done", "incomplete", "failed", "abandoned"},
        "waiting": {"actionable", "dispatched", "done", "incomplete", "failed", "abandoned"},
        "done": {"closed", "superseded"},
        "incomplete": {"closed", "actionable", "abandoned"},
        "closed": {"superseded"},
        "failed": {"dispatched", "proposed", "abandoned"},
        "abandoned": set(), "superseded": set(),
    },
    "knowledge": {
        "proposed": {"assumed", "verified", "done", "abandoned"},
        "assumed": {"verified", "stale", "superseded", "abandoned"},
        "verified": {"stale", "superseded"},
        "done": {"verified", "stale", "superseded"},
        "stale": {"verified", "superseded"},
        "superseded": set(), "abandoned": set(),
    },
    "question": {
        "proposed": {"open", "abandoned"},
        "open": {"answered", "unanswerable", "abandoned"},
        "answered": {"open"}, "unanswerable": {"open"}, "abandoned": set(),
    },
    "verification": {
        "proposed": {"active", "abandoned"},
        "active": {"passed", "failed", "abandoned"},
        "passed": set(), "failed": {"active", "abandoned"}, "abandoned": set(),
    },
}

_NODE_COLUMNS = [
    "id", "work_id", "type", "title", "status", "parent_id", "owner_agent",
    "satisfied_when_kind", "satisfied_when_ref", "side_effect", "requires_approval",
    "authority", "deadline", "wake_kind", "wake_at", "wake_ref", "pod_ref",
    "created_by", "created_at", "updated_at", "content", "payload",
]


def _iso(dt) -> Optional[str]:
    if dt is None or isinstance(dt, str):
        return dt
    return dt.isoformat()


class WorkStore:
    def __init__(self, path: str = "work_objects/work.db", busy_timeout_ms: int = 10_000):
        self.path = path
        # Single-writer model: one connection shared across threads, with ALL access
        # serialized by a reentrant lock, so concurrent managers + a writing curator
        # can never interleave or corrupt the graph. (Reads are serialized too —
        # fine at this scale; thread-local read connections are the later optimization.)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # On a shared DB (dayflow's store lives in emi.db) a write must wait for
        # the main application writer instead of failing "database is locked".
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self._conn.close()

    # ----------------------------- read ----------------------------- #
    def load(self, work_id: str) -> WorkObject:
        with self._lock:
            return self._load(work_id)

    def _load(self, work_id: str) -> WorkObject:
        row = self._conn.execute("SELECT * FROM work_objects WHERE id=?", (work_id,)).fetchone()
        if row is None:
            raise KeyError(f"work_object {work_id!r} not found")
        wo = WorkObject(
            id=row["id"], title=row["title"] or "", goal_node_id=row["goal_node_id"],
            status=row["status"], mission_id=row["mission_id"],
            constraints=json.loads(row["constraints"] or "{}"),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )
        for nrow in self._conn.execute("SELECT * FROM nodes WHERE work_id=?", (work_id,)):
            wo.nodes[nrow["id"]] = self._row_to_node(nrow)
        for erow in self._conn.execute("SELECT * FROM edges WHERE work_id=?", (work_id,)):
            wo.edges.append(Edge(
                id=erow["id"], work_id=erow["work_id"], src=erow["src"], dst=erow["dst"],
                relation=erow["relation"], created_at=erow["created_at"],
                payload=json.loads(erow["payload"] or "{}"),
            ))
        return wo

    def events(self, work_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, actor, op, data FROM events WHERE work_id=? ORDER BY seq", (work_id,)
            ).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "op": r["op"],
                 "data": json.loads(r["data"] or "{}")} for r in rows]

    def list_work_objects(self) -> list[dict[str, Any]]:
        """Summaries of every WorkObject, newest-updated first — for a dashboard / list view."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, title, status, created_at, updated_at FROM work_objects "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [{"id": r["id"], "title": r["title"] or "", "status": r["status"],
                 "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> WorkNode:
        d = {k: row[k] for k in _NODE_COLUMNS}
        d["payload"] = json.loads(d["payload"] or "{}")
        # pydantic coerces ISO strings -> datetime and 0/1 -> bool.
        return WorkNode(**d)

    # ------------------------- write (one entrypoint) ------------------------- #
    def apply(self, op: str, data: dict, actor: Optional[str] = None,
              work_id: Optional[str] = None) -> WorkObject:
        handler = self._HANDLERS.get(op)
        if handler is None:
            raise ValueError(f"unknown op {op!r}")
        now = utcnow().isoformat()
        with self._lock, self._conn:  # serialize writers; atomic event + projection
            if op == "create_work_object":
                wo = handler(self, None, data, now)
            else:
                wid = work_id or data.get("work_id")
                if not wid:
                    raise ValueError(f"op {op!r} requires work_id")
                wo = self._load(wid)
                handler(self, wo, data, now)
            wo.validate()                       # structural invariants
            self._rollup(wo, now)               # derived WorkObject.status
            self._conn.execute(
                "INSERT INTO events(work_id, ts, actor, op, data) VALUES(?,?,?,?,?)",
                (wo.id, now, actor, op, json.dumps(data, default=str)),
            )
            self._persist(wo, now)
        return wo

    # ----------------------------- op handlers ----------------------------- #
    # Each mutates the in-memory wo (validation inline); persistence is shared.
    def _op_create_work_object(self, _wo, data, now) -> WorkObject:
        wo = WorkObject(title=data.get("title", ""), constraints=data.get("constraints", {}),
                        created_at=now, updated_at=now)
        goal = WorkNode(
            work_id=wo.id, type="goal", title=data.get("title", ""), status="proposed",
            satisfied_when_kind=data.get("satisfied_when_kind", "all_owned_children_done"),
            content=data.get("goal_content", ""), created_by=data.get("created_by"),
            created_at=now, updated_at=now,
        )
        wo.add_node(goal)
        wo.goal_node_id = goal.id
        return wo

    def _op_add_node(self, wo, data, now) -> None:
        # nodes.id is the table's GLOBAL primary key. A caller-supplied id that
        # already lives in ANOTHER work object would INSERT OR REPLACE at persist
        # and silently re-home the row — stealing it from the earlier graph and
        # leaving that graph's children/edges dangling. Refuse loudly instead;
        # callers minting meaningful ids must namespace them per work object.
        explicit_id = data.get("id")
        if explicit_id:
            row = self._conn.execute(
                "SELECT work_id FROM nodes WHERE id=?", (str(explicit_id),)
            ).fetchone()
            if row is not None and row["work_id"] != wo.id:
                raise ValueError(
                    f"add_node: id {explicit_id!r} already belongs to work object "
                    f"{row['work_id']!r} — node ids are global; namespace "
                    f"caller-supplied ids per work object"
                )
        parent_id = data.get("parent_id")
        if parent_id is not None and parent_id not in wo.nodes:
            raise ValueError(f"add_node: parent {parent_id!r} not found")
        authority = data.get("authority")
        if parent_id is not None and authority is not None:
            ceiling = wo.nodes[parent_id].authority
            if ceiling is not None and authority > ceiling:
                raise ValueError(f"add_node: authority {authority} exceeds parent ceiling {ceiling}")
        fields = {k: v for k, v in data.items()
                  if k in WorkNode.model_fields and k not in {"work_id", "created_at", "updated_at"}}
        wo.add_node(WorkNode(work_id=wo.id, created_at=now, updated_at=now, **fields))

    def _op_add_edge(self, wo, data, now) -> None:
        if data["src"] not in wo.nodes or data["dst"] not in wo.nodes:
            raise ValueError(f"add_edge: endpoints {data['src']!r}->{data['dst']!r} must exist")
        wo.edges.append(Edge(work_id=wo.id, src=data["src"], dst=data["dst"],
                             relation=data["relation"], payload=data.get("payload", {}),
                             created_at=now))

    def _op_set_status(self, wo, data, now) -> None:
        node = wo.nodes.get(data["node_id"])
        if node is None:
            raise KeyError(f"set_status: node {data['node_id']!r} not found")
        target = data["status"]
        # Incarnation fence (audit W2): every claim (-> dispatched) bumps the
        # node's dispatch_epoch; a writer that captured an older epoch is a
        # ZOMBIE — the sweeper failed its incarnation and repair re-dispatched
        # the node to a successor — and must not write its stale outcome over
        # the live incarnation. Checked only when the caller supplies
        # expected_dispatch_epoch (completion paths); plain status writes
        # (finalizer, sweeper, repair) are unaffected.
        expected = data.get("expected_dispatch_epoch")
        if expected is not None:
            current = int(node.payload.get("dispatch_epoch") or 0)
            if int(expected) != current:
                raise ValueError(
                    f"set_status: stale dispatch epoch {expected} (current {current}) "
                    f"for node {node.id!r} — a newer incarnation owns this node")
        if target != node.status:
            family = FAMILY_BY_TYPE.get(node.type, "spine")
            allowed = TRANSITIONS.get(family, {}).get(node.status, set())
            if target not in allowed:
                raise ValueError(
                    f"illegal transition {node.status!r}->{target!r} for {node.type} ({family})")
        prev = node.status
        node.status = target
        if target == "dispatched" and prev != "dispatched":
            node.payload["dispatch_epoch"] = int(node.payload.get("dispatch_epoch") or 0) + 1
        if data.get("content") is not None:   # optional closing note / evidence written on transition
            node.content = data["content"]
        node.updated_at = now

    def _op_edit_node(self, wo, data, now) -> None:
        """Manual UI edit of a node's title and/or content — no status change, no transition check. For the
        /work editor; only mutates the fields explicitly provided (a missing key leaves that field alone)."""
        node = wo.nodes.get(data["node_id"])
        if node is None:
            raise KeyError(f"edit_node: node {data['node_id']!r} not found")
        if data.get("title") is not None:
            node.title = str(data["title"])
        if data.get("content") is not None:
            node.content = str(data["content"])
        node.updated_at = now

    def _op_attach_pod(self, wo, data, now) -> None:
        node = wo.nodes.get(data["node_id"])
        if node is None:
            raise KeyError(f"attach_pod: node {data['node_id']!r} not found")
        node.pod_ref = data["pod_ref"]
        node.updated_at = now

    def _op_defer_node(self, wo, data, now) -> None:
        node = wo.nodes.get(data["node_id"])
        if node is None:
            raise KeyError(f"defer_node: node {data['node_id']!r} not found")
        wake_kind = data.get("wake_kind")
        if wake_kind is not None and wake_kind not in WAKE_KINDS:
            raise ValueError(f"defer_node: unknown wake_kind {wake_kind!r}")
        node.wake_kind = wake_kind
        node.wake_at = data.get("wake_at")
        node.wake_ref = data.get("wake_ref")
        if node.status == "dispatched":
            node.status = "waiting"
        node.updated_at = now

    def _op_set_work_status(self, wo, data, now) -> None:
        """Force the WorkObject's overall status — the steward's authoritative complete/abandon, distinct
        from the rollup's automatic 'all children done' completion. The forward-only rollup will not reset
        it; the goal node is mirrored to a matching terminal state for consistency."""
        target = data["status"]
        if target not in ("active", "done", "abandoned", "blocked"):
            raise ValueError(f"set_work_status: bad status {target!r}")
        wo.status = target
        goal = wo.nodes.get(wo.goal_node_id or "")
        if goal is not None and target in ("done", "abandoned") and goal.status not in _TERMINAL_STATUSES:
            goal.status = target
            goal.updated_at = now

    # ----------------------------- persistence ----------------------------- #
    def _persist(self, wo: WorkObject, now: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO work_objects"
            "(id,title,goal_node_id,status,mission_id,constraints,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (wo.id, wo.title, wo.goal_node_id, wo.status, wo.mission_id,
             json.dumps(wo.constraints, default=str), _iso(wo.created_at), now),
        )
        placeholders = ",".join("?" * len(_NODE_COLUMNS))
        for n in wo.nodes.values():
            self._conn.execute(
                f"INSERT OR REPLACE INTO nodes({','.join(_NODE_COLUMNS)}) VALUES({placeholders})",
                self._node_params(n),
            )
        for e in wo.edges:
            self._conn.execute(
                "INSERT OR REPLACE INTO edges(id,work_id,src,dst,relation,created_at,payload)"
                " VALUES(?,?,?,?,?,?,?)",
                (e.id, e.work_id, e.src, e.dst, e.relation, _iso(e.created_at),
                 json.dumps(e.payload, default=str)),
            )

    @staticmethod
    def _node_params(n: WorkNode) -> tuple:
        return (
            n.id, n.work_id, n.type, n.title, n.status, n.parent_id, n.owner_agent,
            n.satisfied_when_kind, n.satisfied_when_ref, n.side_effect, int(n.requires_approval),
            n.authority, _iso(n.deadline), n.wake_kind, _iso(n.wake_at), n.wake_ref, n.pod_ref,
            n.created_by, _iso(n.created_at), _iso(n.updated_at), n.content,
            json.dumps(n.payload, default=str),
        )

    def _rollup(self, wo: WorkObject, now: str) -> None:
        """Derived rollup — when the goal is satisfied, close it and the WorkObject.
        Forward-only in v1: reopening a done WorkObject after a regression is an
        explicit curator action (deferred), not an automatic flip-flop here."""
        if wo.status == "abandoned":      # a force-abandoned WorkObject is terminal — never auto-complete it
            wo.updated_at = now
            return
        goal = wo.nodes.get(wo.goal_node_id or "")
        if goal is not None and wo.is_satisfied(goal):
            wo.status = "done"
            if goal.status == "dispatched":      # close the goal node (dispatched->done is legal)
                goal.status = "done"
                goal.updated_at = now
        wo.updated_at = now

    _HANDLERS: dict[str, Callable] = {}


WorkStore._HANDLERS = {
    "create_work_object": WorkStore._op_create_work_object,
    "add_node": WorkStore._op_add_node,
    "add_edge": WorkStore._op_add_edge,
    "set_status": WorkStore._op_set_status,
    "edit_node": WorkStore._op_edit_node,
    "set_work_status": WorkStore._op_set_work_status,
    "attach_pod": WorkStore._op_attach_pod,
    "defer_node": WorkStore._op_defer_node,
}
