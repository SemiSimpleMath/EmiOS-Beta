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
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from work_objects.model import (
    WAKE_KINDS, ActionRecord, Edge, SCHEMA_SQL, WorkNode, WorkObject,
    _STARTABLE_STATUSES, _TERMINAL_STATUSES, utcnow,
)

logger = logging.getLogger(__name__)


def _held_by_future_wake(node) -> bool:
    """True when the node's wake_at lies in the FUTURE — it is held on purpose until its
    time (the churn fence's second protected state). Tolerates the in-memory str form a
    just-deferred node can carry before reload; an unparseable wake reads as not held."""
    wa = node.wake_at
    if wa is None:
        return False
    if isinstance(wa, str):
        try:
            wa = datetime.fromisoformat(wa)
        except ValueError:
            return False
    if wa.tzinfo is None:
        wa = wa.replace(tzinfo=timezone.utc)
    return wa > utcnow()


def _cascade_abandon_startable(wo: WorkObject, now: str, reason: str) -> int:
    """Closure obligation: a terminal WorkObject may contain nothing the engine could ever
    start again. Abandon every startable node and clear its wake so no timer or promotion
    path can resurrect it (validate() enforces the invariant). In-flight ``dispatched``
    WORKER nodes are left to land their result — inert in a terminal object, and the sweep
    skips terminal work objects. A dispatched ASK (wake_kind=user_reply) has no thread and
    no result to land — the object's closure moots the question, so it cascades too (its
    ticket dies on its own valid_until). Idempotent; returns the number of nodes cascaded."""
    count = 0
    for node in wo.nodes.values():
        if node.status in _STARTABLE_STATUSES or (
                node.status == "dispatched" and node.wake_kind == "user_reply"):
            node.status = "abandoned"
            node.wake_kind = None
            node.wake_at = None
            node.wake_ref = None
            node.payload["terminal"] = {"status": "abandoned", "verdict": "cascade",
                                        "reason": reason, "at": now}
            node.updated_at = now
            count += 1
    return count

def _descendants(wo: WorkObject, node_id: str) -> list:
    """Every node owned below `node_id` on the parent_id tree. Cycle-safe."""
    out, frontier, seen = [], [node_id], {node_id}
    while frontier:
        parent = frontier.pop()
        for n in wo.nodes.values():
            if n.parent_id == parent and n.id not in seen:
                seen.add(n.id)
                out.append(n)
                frontier.append(n.id)
    return out


def _cascade_abandon_subtree(wo: WorkObject, node_id: str, now: str, reason: str) -> int:
    """A finished node's unstarted descendants stop with it.

    2026-09-13: a research node was judged complete by the finalizer and CLOSED, with a
    correct epitaph naming the answer it had found. Eight of its descendants kept running
    anyway, spawning further children, ten levels down, because nothing connected a
    parent's outcome to its subtree. The goal reached 117 nodes while every supervising
    agent saw "progress: 1/2". A node that has declared its outcome cannot have that
    outcome changed by children that have not started, so they are abandoned.

    Same shape as `_cascade_abandon_startable` but scoped to one subtree, and with the same
    exemption: an in-flight ``dispatched`` WORKER node is left to land its result rather
    than being orphaned mid-call. A dispatched ASK (wake_kind=user_reply) has no thread and
    no result to land, so the parent's completion moots it and it cascades.
    """
    count = 0
    for node in _descendants(wo, node_id):
        if node.status in _STARTABLE_STATUSES or (
                node.status == "dispatched" and node.wake_kind == "user_reply"):
            node.status = "abandoned"
            node.wake_kind = None
            node.wake_at = None
            node.wake_ref = None
            node.payload["terminal"] = {"status": "abandoned", "verdict": "parent_finished",
                                        "reason": reason, "at": now}
            node.updated_at = now
            count += 1
    return count


# Reaching one of these means the node has declared its outcome; its unstarted subtree is
# moot. `done` is included deliberately: it is the worker's own verdict on its own node, and
# the runaway leaked through exactly there — descendants of `done` ancestors were still
# minting children an hour later.
_SUBTREE_CASCADE_STATUSES = {"done", "closed", "abandoned", "superseded"}

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
        # dispatched = in-flight (a worker is on it, or an ask is out) — the action_selector/gate are blind
        # to it. A surfaced ask IS dispatched (wake_kind=user_reply marks it): the reply is its result
        # (-> done); an expired-unanswered ticket is a timed-out tool call (sweeper -> failed, work_repair
        # adjudicates). There is no re-ask timer.
        # done = a tool result was recorded for this node; the work_finalizer has not judged it yet, and
        # ALONE produces closed (the satisfied terminal). What actually happened — including "acted on but
        # couldn't" — lives in the result TEXT, not in a status of its own. [is_satisfied keys on closed]
        "proposed": {"actionable", "dispatched", "waiting", "failed", "abandoned"},   # failed = dispatch broke before the node ran
        "actionable": {"dispatched", "waiting", "failed", "abandoned"},   # state_mover-promoted; awaiting action_selector dispatch
        "dispatched": {"waiting", "done", "failed", "abandoned"},
        "waiting": {"actionable", "dispatched", "done", "failed", "abandoned"},
        "done": {"closed", "superseded"},
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
        "dispatched": {"waiting", "done", "failed", "abandoned"},
        "waiting": {"actionable", "dispatched", "done", "failed", "abandoned"},
        "done": {"closed", "superseded"},
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
        # ORDER BY rowid: node iteration order is load-bearing for the task runtime's
        # facts_context (last-writer-wins on a repeated data_id) — make it insertion order
        # by contract, not by rowid accident (verification finding F10).
        for nrow in self._conn.execute("SELECT * FROM nodes WHERE work_id=? ORDER BY rowid", (work_id,)):
            wo.nodes[nrow["id"]] = self._row_to_node(nrow)
        for arow in self._conn.execute(
                "SELECT * FROM actions WHERE work_id=? ORDER BY ts", (work_id,)):
            wo.actions.append(ActionRecord(
                id=arow["id"], work_id=arow["work_id"], node_id=arow["node_id"],
                ts=arow["ts"], channel=arow["channel"], target=arow["target"] or "",
                summary=arow["summary"] or "", outcome=arow["outcome"] or "sent",
                actor=arow["actor"], payload=json.loads(arow["payload"] or "{}"),
            ))
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

    def repair_terminal_zombies(self) -> int:
        """One-time data repair (2026-07-30 zombie-wake incident): cascade-abandon startable
        nodes left inside already-terminal work objects by the pre-cascade closure code —
        exactly what _op_set_work_status now does at closure. Must run before normal writes:
        any apply() touching a zombie object would otherwise fail validate() on the old rows.
        Idempotent; returns the number of nodes repaired (0 once clean — a nonzero return
        after the first run means some writer bypassed the invariant and should be found)."""
        placeholders = ",".join("?" * len(_STARTABLE_STATUSES))
        with self._lock, self._conn:
            rows = self._conn.execute(
                f"SELECT DISTINCT w.id, w.status FROM work_objects w "
                f"JOIN nodes n ON n.work_id = w.id "
                f"WHERE w.status IN ('done','abandoned') AND n.status IN ({placeholders})",
                tuple(_STARTABLE_STATUSES),
            ).fetchall()
            if not rows:
                return 0
            now = utcnow().isoformat()
            repaired = 0
            for r in rows:
                wo = self._load(r["id"])
                count = _cascade_abandon_startable(wo, now, reason=f"work_object_{r['status']}")
                repaired += count
                self._conn.execute(
                    "INSERT INTO events(work_id, ts, actor, op, data) VALUES(?,?,?,?,?)",
                    (wo.id, now, "store_repair", "cascade_closure_repair",
                     json.dumps({"cascaded": count})),
                )
                self._persist(wo, now)
        logger.warning(
            "[WorkStore] closure-cascade repair: abandoned %d startable node(s) inside %d "
            "terminal work object(s)", repaired, len(rows))
        return repaired

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
                wo = handler(self, None, data, now, actor)
            else:
                wid = work_id or data.get("work_id")
                if not wid:
                    raise ValueError(f"op {op!r} requires work_id")
                wo = self._load(wid)
                handler(self, wo, data, now, actor)
            self._rollup(wo, now)               # derived WorkObject.status (auto-close cascades)
            wo.validate()                       # invariants — after rollup so they see the final state
            self._conn.execute(
                "INSERT INTO events(work_id, ts, actor, op, data) VALUES(?,?,?,?,?)",
                (wo.id, now, actor, op, json.dumps(data, default=str)),
            )
            self._persist(wo, now)
        return wo

    # ----------------------------- op handlers ----------------------------- #
    # Each mutates the in-memory wo (validation inline); persistence is shared.
    def _op_create_work_object(self, _wo, data, now, actor=None) -> WorkObject:
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

    def _op_add_node(self, wo, data, now, actor=None) -> None:
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
        node = WorkNode(work_id=wo.id, created_at=now, updated_at=now, **fields)
        # Ownership inherits down the parent_id spine BY CONSTRUCTION: a node grown
        # under a session-owned parent carries the same payload.session_id, so the
        # supervisor's liveness join is one lookup — no ancestor walk (work-session
        # rewrite; replaces the 2026-08-04 ancestor-liveness patch class entirely).
        if parent_id is not None and "session_id" not in node.payload:
            parent_sid = wo.nodes[parent_id].payload.get("session_id")
            if parent_sid:
                node.payload["session_id"] = parent_sid
        wo.add_node(node)

    def _op_add_edge(self, wo, data, now, actor=None) -> None:
        if data["src"] not in wo.nodes or data["dst"] not in wo.nodes:
            raise ValueError(f"add_edge: endpoints {data['src']!r}->{data['dst']!r} must exist")
        wo.edges.append(Edge(work_id=wo.id, src=data["src"], dst=data["dst"],
                             relation=data["relation"], payload=data.get("payload", {}),
                             created_at=now))

    def _op_set_status(self, wo, data, now, actor=None) -> None:
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
        # FAILED NODES ARE WORK_REPAIR'S (2026-09-02 spend-alert loop). The design
        # (ask redesign 2026-08-18) makes an expired ask a timed-out tool call: the
        # sweeper fails the node and REPAIR adjudicates — escalate / retry / abandon
        # is a repair decision. But the architect runs BEFORE repair in the tick and
        # its prompt claimed failed nodes "come back to you", so it consumed each
        # failure as a planning problem: abandon + mint a fresh identical ask,
        # hourly, nine times — repair never saw one. The prompt now says failed
        # nodes are repair's; this fence is the wall behind the words. A licensed
        # replan (finalizer amend / user directive) still may prune a failed node.
        if node.status == "failed" and actor == "architect" and not data.get("licensed"):
            raise ValueError(
                f"set_status: node {node.id!r} is 'failed' — failed nodes are "
                f"work_repair's to adjudicate (retry / escalate / abandon). Plan "
                f"around it and read repair's verdict; only a licensed replan may "
                f"prune it.")
        if target != node.status:
            family = FAMILY_BY_TYPE.get(node.type, "spine")
            allowed = TRANSITIONS.get(family, {}).get(node.status, set())
            if target not in allowed:
                raise ValueError(
                    f"illegal transition {node.status!r}->{target!r} for {node.type} ({family})")
        # FINALIZER HAS FINAL SAY (owner ruling 2026-08-10): every node ENDING goes
        # through this one chokepoint and must carry its epitaph. A terminal write
        # without a reason is refused — the graph records WHY, not just what, so the
        # next planning pass reads epitaphs instead of a mute corpse pile (the
        # AC-object replan loop: 187 reasonless abandons).
        _TERMINAL_TARGETS = {"closed", "abandoned", "superseded"}
        if target in _TERMINAL_TARGETS and target != node.status:
            # IN-FLIGHT ASK FENCE (2026-08-18 walk-storm): a dispatched ask
            # (wake_kind == "user_reply") is an in-progress tool call — the
            # question is out to the user and the reply is its result. A
            # terminal write here orphans that reply. The ask ends like any
            # tool call: the reply/dismiss lands it -> done, an expired
            # ticket times it out (sweeper -> failed, work_repair decides),
            # or the whole object closes (the cascade clears it without
            # passing through here).
            # CHURN FENCE (2026-08-22, owner design): queued/held work is the
            # runtime's. `actionable` = approved and QUEUED for dispatch (one node
            # per tick); `waiting` with a FUTURE wake = held on purpose until its
            # time. Neither is stalled — and silence cannot kill them: 33
            # generations of a delivery node died to replan churn because "hasn't
            # run yet" read as "will never run". Abandoning such a node requires a
            # LICENSED replan — evidence (a finalizer amend) or a user directive
            # (steward-classed) — carried as data["licensed"] by the replan
            # applier. The closure cascade clears these without passing through
            # here; repair adjudicates `failed` nodes, which are never in this set.
            if (target == "abandoned" and not data.get("licensed")
                    and (node.status == "actionable"
                         or (node.status == "waiting" and _held_by_future_wake(node)))):
                raise ValueError(
                    f"set_status: node {node.id!r} is {node.status!r} — queued/held work is the "
                    f"runtime's and WILL run (queued is not stalled). Abandoning it requires a "
                    f"licensed replan (finalizer amend or user directive). Depend on it or leave it.")
            if node.status == "dispatched" and node.wake_kind == "user_reply":
                raise ValueError(
                    f"set_status: node {node.id!r} is an in-flight ask (question out to "
                    f"the user) — terminal {target!r} refused; it ends by reply, "
                    f"dismissal, or ticket timeout")
            reason = str(data.get("reason") or "").strip()
            if not reason:
                raise ValueError(
                    f"set_status: terminal transition to {target!r} for node "
                    f"{node.id!r} requires a non-empty 'reason'")
            node.payload["terminal"] = {
                "status": target,
                "verdict": str(data.get("verdict") or target),
                "reason": reason,
                "at": now,
            }
        prev = node.status
        node.status = target
        if target == "dispatched" and prev != "dispatched":
            node.payload["dispatch_epoch"] = int(node.payload.get("dispatch_epoch") or 0) + 1
        if target == "failed" and prev != "failed":
            # HOW MANY TIMES THIS STEP HAS NOW FAILED. Nothing counted failures, so nothing could
            # notice a step failing the same way forever: one delivery node recorded the SAME tool
            # error seventeen times and every projection read it as seventeen sub-nodes of
            # progress. Counted here, on the node, because this is the one chokepoint every
            # failure passes through — and counted by id, never by comparing error text.
            node.payload["failure_count"] = int(node.payload.get("failure_count") or 0) + 1
        if data.get("session_id") is not None:
            # Ownership is a graph fact (work-session rewrite): the discharging session
            # stamps itself on the node; the supervisor reads this, not a registry.
            node.payload["session_id"] = str(data["session_id"])
        if data.get("content") is not None:   # optional closing note / evidence written on transition
            node.content = data["content"]
        if target in _SUBTREE_CASCADE_STATUSES and prev != target:
            # The node has declared its outcome. Anything below it that has not started
            # cannot change that outcome, so it stops here rather than growing a new
            # branch under a finished parent (see _cascade_abandon_subtree).
            stopped = _cascade_abandon_subtree(
                wo, node.id, now,
                reason=f"parent {node.id} reached {target}: unstarted work below it is moot")
            if stopped:
                logger.info("[WorkStore] %s -> %s cascaded %d unstarted descendant(s)",
                            node.id, target, stopped)
        if data.get("finalizer") is not None:
            # THE FINALIZER'S VERDICT, ON THE NODE IT JUDGED. Its instruction has to reach the
            # architect, which runs on a LATER tick — and every tick builds a fresh manager with a
            # fresh Blackboard (MultiAgentManager.__init__), so a handoff left in memory at the tail
            # of a tick is thrown away before the reader exists. The graph is the return channel for
            # results; it is the return channel for judgments too. Sits beside `terminal` rather
            # than in `content`, which is the node's immutable directive.
            node.payload["finalizer"] = {
                "verdict": str(data["finalizer"].get("verdict") or ""),
                "instruction": str(data["finalizer"].get("instruction") or ""),
                "reasoning": str(data["finalizer"].get("reasoning") or ""),
                "at": now,
            }
        if data.get("note") is not None:
            # Append-only lifecycle note (e.g. the sweeper's timeout reason).
            # Lives in the payload so the node's `content` — its immutable
            # directive — is never clobbered by status bookkeeping (the
            # 2026-08-19 ticket-garbage incident: a timeout reason overwrote a
            # deliver node's directive and every later surface rendered it).
            node.payload.setdefault("status_notes", []).append(
                {"note": str(data["note"]), "at": now})
        node.updated_at = now

    def _op_edit_node(self, wo, data, now, actor=None) -> None:
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

    def _op_consume_finalizer_instruction(self, wo, data, now, actor=None) -> None:
        """Mark a finalizer instruction as acted on, so the architect reads it exactly once.

        Without this the instruction sits on the node forever and every later re-plan of the
        same work object re-applies a judgment about a step that was dealt with ticks ago.
        Stamped rather than deleted: the node keeps the record of what was decided and when."""
        node = wo.nodes.get(data["node_id"])
        if node is None:
            raise KeyError(f"consume_finalizer_instruction: node {data['node_id']!r} not found")
        entry = node.payload.get("finalizer")
        if not isinstance(entry, dict):
            raise ValueError(
                f"consume_finalizer_instruction: node {node.id!r} carries no finalizer instruction")
        entry["consumed_at"] = now
        node.updated_at = now

    def _op_attach_pod(self, wo, data, now, actor=None) -> None:
        node = wo.nodes.get(data["node_id"])
        if node is None:
            raise KeyError(f"attach_pod: node {data['node_id']!r} not found")
        node.pod_ref = data["pod_ref"]
        node.updated_at = now

    def _op_defer_node(self, wo, data, now, actor=None) -> None:
        node = wo.nodes.get(data["node_id"])
        if node is None:
            raise KeyError(f"defer_node: node {data['node_id']!r} not found")
        wake_kind = data.get("wake_kind")
        if wake_kind is not None and wake_kind not in WAKE_KINDS:
            raise ValueError(f"defer_node: unknown wake_kind {wake_kind!r}")
        node.wake_kind = wake_kind
        node.wake_at = data.get("wake_at")
        node.wake_ref = data.get("wake_ref")
        # A worker deferring its in-flight node parks it (dispatched -> waiting).
        # A user_reply wake is the exception: it marks an in-flight ASK — the
        # question is out, the node STAYS dispatched until reply or timeout.
        if node.status == "dispatched" and wake_kind != "user_reply":
            node.status = "waiting"
        node.updated_at = now

    def _op_record_action(self, wo, data, now, actor=None) -> None:
        """Append one outward-facing act to this work object's ledger.

        Called by the TOOL that performs the side effect, at the moment it happens.
        Deliberately unvalidated against node state: an act that reached the outside
        world is a fact, and a ledger that can refuse a fact is worse than none.
        `outcome` may be revised later by appending a new row, never by editing this one.
        """
        channel = str(data.get("channel") or "").strip()
        if not channel:
            raise ValueError("record_action: 'channel' is required (email | ticket | sms | ...)")
        wo.actions.append(ActionRecord(
            work_id=wo.id,
            node_id=(str(data["node_id"]) if data.get("node_id") else None),
            ts=now,
            channel=channel,
            target=str(data.get("target") or ""),
            summary=" ".join(str(data.get("summary") or "").split()),
            outcome=str(data.get("outcome") or "sent"),
            actor=actor or str(data.get("actor") or "") or None,
            payload=data.get("payload") or {},
        ))

    def _op_set_work_status(self, wo, data, now, actor=None) -> None:
        """Force the WorkObject's overall status — the steward's authoritative complete/abandon, distinct
        from the rollup's automatic 'all children done' completion. The forward-only rollup will not reset
        it. Entering a terminal status is a transition with obligations, not a label write: the goal node
        is mirrored terminal and every still-startable node is cascade-abandoned with its wake cleared,
        so a closed object can never fire again (validate() enforces)."""
        target = data["status"]
        if target not in ("active", "done", "abandoned", "blocked"):
            raise ValueError(f"set_work_status: bad status {target!r}")
        if target in ("done", "abandoned"):
            reason = str(data.get("reason") or "").strip()
            if not reason:
                raise ValueError(
                    f"set_work_status: terminal status {target!r} for {wo.id!r} "
                    f"requires a non-empty 'reason'")
            wo.constraints["terminal"] = {"status": target, "reason": reason, "at": now}
        wo.status = target
        if target in ("done", "abandoned"):
            goal = wo.nodes.get(wo.goal_node_id or "")
            if goal is not None and goal.status not in _TERMINAL_STATUSES:
                goal.status = target
                goal.payload["terminal"] = {"status": target, "verdict": "work_object_terminal",
                                            "reason": str(data.get("reason")), "at": now}
                goal.updated_at = now
            _cascade_abandon_startable(wo, now, reason=f"work_object_{target}: {data.get('reason')}")

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
        for a in wo.actions:
            self._conn.execute(
                "INSERT OR REPLACE INTO actions"
                "(id,work_id,node_id,ts,channel,target,summary,outcome,actor,payload)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (a.id, a.work_id, a.node_id, _iso(a.ts), a.channel, a.target,
                 a.summary, a.outcome, a.actor, json.dumps(a.payload, default=str)),
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

    @staticmethod
    def _unconsumed_finalizer_instruction(wo: WorkObject) -> Optional[str]:
        """The id of a node still asking the architect to change this plan, if any.

        A finalizer `amend` or `replan` says the plan must change; the architect that acts on it
        runs a LATER tick. Auto-completion must not win that race — an `amend` on the LAST node of
        a goal would otherwise be destroyed by the completion it triggers, and the instruction
        written for the architect becomes unreachable (the reader scans ACTIVE objects only).

        2026-09-17: a lights goal closed as `done` carrying its own epitaph, "the result does not
        show that the lights were actually turned off". The judgment was right and the rollup
        overrode it. Only the automatic rollup defers — the steward's explicit
        `set_work_status` stays authoritative, because a person deciding a goal is over outranks a
        pending note about how to continue it.
        """
        for node in wo.nodes.values():
            entry = (node.payload or {}).get("finalizer")
            if not isinstance(entry, dict) or entry.get("consumed_at"):
                continue
            if str(entry.get("instruction") or "").strip():
                return node.id
        return None

    def _rollup(self, wo: WorkObject, now: str) -> None:
        """Derived rollup — when the goal is satisfied, close it and the WorkObject.
        Forward-only in v1: reopening a done WorkObject after a regression is an
        explicit curator action (deferred), not an automatic flip-flop here.
        Auto-completion carries the same closure obligation as set_work_status:
        startable stragglers (nodes outside the satisfied goal subtree) are
        cascade-abandoned so the done object can never fire again."""
        if wo.status == "abandoned":      # a force-abandoned WorkObject is terminal — never auto-complete it
            wo.updated_at = now
            return
        goal = wo.nodes.get(wo.goal_node_id or "")
        if goal is not None and wo.is_satisfied(goal):
            pending = self._unconsumed_finalizer_instruction(wo)
            if pending is not None:
                logger.info(
                    "[WorkStore] %s is satisfied but holding: %s carries an unconsumed finalizer "
                    "instruction — the architect re-plans it before this goal can complete.",
                    wo.id, pending)
                wo.updated_at = now
                return
            wo.status = "done"
            if goal.status == "dispatched":      # close the goal node (dispatched->done is legal)
                goal.status = "done"
                goal.updated_at = now
            _cascade_abandon_startable(wo, now, reason="work_object_done")
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
    "consume_finalizer_instruction": WorkStore._op_consume_finalizer_instruction,
    "defer_node": WorkStore._op_defer_node,
    "record_action": WorkStore._op_record_action,
}
