"""
work_objects.model — the WorkGraph data model (nodes, edges, the WorkObject).

Architecture rules (so this survives new node types without churn):

  * GENERIC, not type-per-table. `type` / `status` / edge `relation` are OPEN
    strings. The known values live in the *_KNOWN sets below as documentation /
    switch helpers, but nothing constrains the column — a NEW node type or
    relation needs ZERO schema change.

  * FIRST-CLASS COLUMNS for everything the engine queries / filters / joins
    (the scheduler, the writer, the authority gate): status, wake_*,
    satisfied_when_*, authority, side_effect, requires_approval, deadline,
    parent_id, provenance. Discriminator:
        does the engine query it?  -> column
        does only an LLM read it?  -> `content` (NL) or `payload` (typed bag)
    Type-specific fields (a Tool's args, a quality_bar's threshold, an Evidence
    node's confidence) go in `payload` — that JSON is *natural* there, not awkward.

  * OWNERSHIP IS A TREE on `parent_id` — each node has <=1 parent, roots are
    NULL, the tree is acyclic. This single spine carries decomposition + the
    authority/budget ceiling that flows DOWN. Edges are NEVER used for ownership
    (no dual representation, so the two can never drift).

  * DEPENDENCY / KNOWLEDGE IS A DAG of EDGES (their own table), never JSON arrays
    on a node — the ready-set and dependents queries are the hot path and must be
    relational + indexed. depends_on / produces / verifies / answers / supports /
    supersedes / references are edges. A node owned ONCE (parent_id) can be reused
    by MANY via edges (e.g. one Evidence node supports many).

  * The append-only `events` log is the SOURCE OF TRUTH; `nodes`/`edges` are a
    rebuildable projection. The validated writer (store.py, next) appends events
    and updates the projection.

  * ready / blocked / stale are DERIVED (computed below), never stored.

If an engine-load-bearing field is later discovered, it's an additive
`ALTER TABLE ... ADD COLUMN` (cheap), not a redesign.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Open vocabularies — known values only; the model fields stay plain `str`.
# --------------------------------------------------------------------------- #
NODE_TYPES_KNOWN = {
    "goal", "plan", "subtask", "tool",          # work spine
    "notify",                                    # legacy rows only — new graphs mint plain nodes and the
                                                 # switchboard reads each node's GOAL to route it
    "evidence", "artifact",                      # knowledge
    "question",                                  # open loop
    "verification",                              # check
}
# DAG cross-links ONLY — ownership is not here (it's the parent_id tree).
EDGE_RELATIONS_KNOWN = {
    "depends_on", "produces", "answers", "verifies",
    "supersedes", "supports", "contradicts", "references",
}
NODE_STATUS_KNOWN = {
    # work spine
    "proposed", "actionable", "dispatched", "waiting", "done", "incomplete", "closed", "failed", "abandoned",
    # knowledge (evidence/artifact)
    "assumed", "verified", "stale", "superseded",
    # question / verification ("active" = a verification run in progress)
    "open", "answered", "unanswerable", "active", "passed",
}
SATISFIED_WHEN_KINDS = {
    "tool_success", "all_owned_children_done", "verified_by", "user_signoff", "quality_bar",
}
SIDE_EFFECTS = {"read", "mutate", "irreversible"}
WAKE_KINDS = {"time", "event", "user_reply", "signal"}

# statuses that count as "this node has delivered what it owes". `closed` (not `done`) is the spine
# terminal: a worker-`done` node has only produced a RESULT — the finalizer must judge it and close it
# before it counts toward the goal (the done->closed gate; see work_finalizer).
_SATISFIED_STATUSES = {"closed", "verified", "passed", "answered"}
# statuses past which a node will never become ready again
_TERMINAL_STATUSES = {"done", "closed", "failed", "abandoned", "superseded", "verified", "passed", "answered", "unanswerable"}
# statuses from which the engine can still START work — is_ready promotes proposed/waiting/
# actionable (wake arming reads the same set) and work_repair re-issues failed. A terminal
# WorkObject must contain none: closing cascades them to abandoned (store._op_set_work_status /
# _rollup) and validate() enforces the invariant, so a closed object can never fire again.
_STARTABLE_STATUSES = {"proposed", "actionable", "waiting", "failed"}


def new_id(prefix: str = "node") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Node
# --------------------------------------------------------------------------- #
class WorkNode(BaseModel):
    # extra=forbid: top-level fields are explicit (engine tier). Anything
    # type-specific belongs in `payload`, which is open by design.
    model_config = ConfigDict(extra="forbid")

    # --- identity / structure ---
    id: str = Field(default_factory=lambda: new_id("node"))
    work_id: str
    type: str                                    # open; see NODE_TYPES_KNOWN
    title: str = ""
    status: str = "proposed"                     # open; see NODE_STATUS_KNOWN
    parent_id: Optional[str] = None              # ownership spine: <=1 parent, NULL at root, acyclic

    # --- contract (engine-queried -> first-class) ---
    owner_agent: Optional[str] = None
    satisfied_when_kind: Optional[str] = None    # see SATISFIED_WHEN_KINDS
    satisfied_when_ref: Optional[str] = None     # e.g. a verification node id
    side_effect: str = "read"                    # see SIDE_EFFECTS
    requires_approval: bool = False
    authority: Optional[int] = None              # ceiling; None = inherit parent
    # per-node budget is deferred (v1 enforces at root, in WorkObject.constraints).
    # when it lands it rides the SAME parent_id ceiling channel as authority and
    # becomes additive columns — the tree already leaves room; no node owns budget alone.
    deadline: Optional[datetime] = None

    # --- waits / resume (the park/wake hot path -> first-class + indexed) ---
    wake_kind: Optional[str] = None              # see WAKE_KINDS
    wake_at: Optional[datetime] = None           # for wake_kind == "time"
    wake_ref: Optional[str] = None               # event/signal id, question id, ...

    # --- data nodes ---
    pod_ref: Optional[str] = None                # datapod:* for evidence/artifact

    # --- provenance ---
    created_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # --- open tiers: NL body + type-specific bag (NEVER engine-queried) ---
    content: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: new_id("edge"))
    work_id: str
    src: str                                     # node id
    dst: str                                     # node id
    relation: str                                # open; see EDGE_RELATIONS_KNOWN
    created_at: datetime = Field(default_factory=utcnow)
    # open bag for relational metadata (e.g. supersedes: reason / actor / confidence).
    # the create-edge event also records actor+ts; this is for queryable edge facts.
    payload: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# WorkObject (the graph container)
# --------------------------------------------------------------------------- #
class WorkObject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: new_id("work"))
    title: str = ""
    goal_node_id: Optional[str] = None
    status: str = "active"                        # active | done | abandoned | blocked
    mission_id: Optional[str] = None
    constraints: dict[str, Any] = Field(default_factory=dict)  # goal-level budget/deadline/values
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # the in-memory projection (store.py loads/persists these)
    nodes: dict[str, WorkNode] = Field(default_factory=dict)
    edges: list[Edge] = Field(default_factory=list)

    # ---- structural construction (the validated writer wraps these with events) ----
    def add_node(self, node: WorkNode) -> WorkNode:
        if node.work_id != self.id:
            raise ValueError(f"node.work_id {node.work_id!r} != work {self.id!r}")
        if node.id in self.nodes:
            raise ValueError(f"duplicate node id {node.id!r}")
        self.nodes[node.id] = node
        return node

    def add_edge(self, src: str, dst: str, relation: str) -> Edge:
        if src not in self.nodes or dst not in self.nodes:
            raise ValueError(f"edge endpoints must exist: {src!r}->{dst!r}")
        edge = Edge(work_id=self.id, src=src, dst=dst, relation=relation)
        self.edges.append(edge)
        return edge

    # ---- derived queries (nothing here is stored) ----
    def deps_of(self, node_id: str) -> list[str]:
        """node ids this node depends_on."""
        return [e.src for e in self.edges if e.dst == node_id and e.relation == "depends_on"]

    def children_of(self, node_id: str) -> list[str]:
        """nodes this one OWNS (parent_id tree) — not dependency links."""
        return [n.id for n in self.nodes.values() if n.parent_id == node_id]

    def is_satisfied(self, node: WorkNode) -> bool:
        # A worker's OWN checklist child (parent != goal) is final at `done`: the
        # finalizer judges only TOP-LEVEL nodes, so `done` is the last state any
        # machine ever gives a child. Requiring `closed` of one made every
        # dependency wired into a worker subtree permanently unsatisfiable — the
        # "blocked by obsolete dependencies" replan churn (2026-08-22): the
        # architect's flat graph render invites deps onto result-bearing children,
        # which then never satisfy. Goal-counting is untouched: top-level nodes
        # (parent == goal) still require the finalizer's `closed`.
        if (node.type == "subtask" and node.status == "done"
                and node.parent_id and node.parent_id != self.goal_node_id):
            return True
        kind = node.satisfied_when_kind
        if kind == "tool_success":
            return node.status == "closed"   # finalizer-closed, not raw worker `done`
        if kind == "user_signoff":
            return node.status == "closed"
        if kind == "all_owned_children_done":
            kids = [self.nodes[c] for c in self.children_of(node.id) if c in self.nodes]
            return bool(kids) and all(self.is_satisfied(k) for k in kids)
        if kind == "verified_by":
            ref = self.nodes.get(node.satisfied_when_ref or "")
            return ref is not None and ref.status == "passed"
        # quality_bar -> deferred to v2; default -> a plain terminal-good status.
        return node.status in _SATISFIED_STATUSES

    def is_ready(self, node: WorkNode, now: Optional[datetime] = None) -> bool:
        # proposed/waiting = gate-checkable (state_mover reads this to decide promotion);
        # actionable = already state_mover-promoted but not yet dispatched — still "ready".
        if node.status not in {"proposed", "waiting", "actionable"}:
            return False
        now = now or utcnow()
        if node.wake_at is not None and node.wake_at > now:
            return False
        for dep_id in self.deps_of(node.id):
            dep = self.nodes.get(dep_id)
            if dep is None or not self.is_satisfied(dep):
                return False
        return True

    def ready_nodes(self, now: Optional[datetime] = None) -> list[WorkNode]:
        now = now or utcnow()
        return [n for n in self.nodes.values() if self.is_ready(n, now)]

    def blocked_nodes(self, now: Optional[datetime] = None) -> list[WorkNode]:
        now = now or utcnow()
        out = []
        for n in self.nodes.values():
            if n.status in {"proposed", "waiting"} and not self.is_ready(n, now):
                out.append(n)
        return out

    def validate(self) -> None:
        """Enforce invariants (the writer calls this after each patch):
        every parent_id / edge endpoint resolves, the ownership tree is acyclic, and a
        terminal WorkObject contains no startable node (closure must have cascaded them —
        the 2026-07-30 zombie-wake incident: a `waiting` node inside a `done` object kept
        an armed timer and fired a ghost ticket a day after the object closed).
        Single-parent is guaranteed structurally — parent_id is one field."""
        for n in self.nodes.values():
            if n.parent_id is not None and n.parent_id not in self.nodes:
                raise ValueError(f"node {n.id}: parent_id {n.parent_id!r} not found")
        for start in self.nodes.values():
            cur, steps = start, 0
            while cur.parent_id is not None:
                cur = self.nodes[cur.parent_id]
                steps += 1
                if steps > len(self.nodes):
                    raise ValueError(f"ownership cycle reachable from {start.id!r}")
        for e in self.edges:
            if e.src not in self.nodes or e.dst not in self.nodes:
                raise ValueError(f"edge {e.id}: endpoints {e.src!r}->{e.dst!r} not found")
        if self.status in ("done", "abandoned"):
            startable = sorted(n.id for n in self.nodes.values() if n.status in _STARTABLE_STATUSES)
            if startable:
                raise ValueError(
                    f"terminal work object {self.id!r} ({self.status}) contains startable "
                    f"node(s) {startable} — closing a work object must cascade-abandon them")


# --------------------------------------------------------------------------- #
# Persistence schema (own SQLite — NOT unified_log). store.py applies this.
# Generic tables: a new node type / relation needs no migration; a newly
# engine-load-bearing field is an additive ALTER TABLE ADD COLUMN.
# --------------------------------------------------------------------------- #
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS work_objects (
    id           TEXT PRIMARY KEY,
    title        TEXT,
    goal_node_id TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    mission_id   TEXT,
    constraints  TEXT,            -- json
    created_at   TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id                  TEXT PRIMARY KEY,
    work_id             TEXT NOT NULL,
    type                TEXT NOT NULL,
    title               TEXT,
    status              TEXT NOT NULL DEFAULT 'proposed',
    parent_id           TEXT,            -- ownership tree (<=1 parent, NULL=root, acyclic)
    owner_agent         TEXT,
    satisfied_when_kind TEXT,
    satisfied_when_ref  TEXT,
    side_effect         TEXT NOT NULL DEFAULT 'read',
    requires_approval   INTEGER NOT NULL DEFAULT 0,
    authority           INTEGER,
    deadline            TEXT,
    wake_kind           TEXT,
    wake_at             TEXT,
    wake_ref            TEXT,
    pod_ref             TEXT,
    created_by          TEXT,
    created_at          TEXT,
    updated_at          TEXT,
    content             TEXT,
    payload             TEXT             -- json: type-specific, LLM-read only
);
CREATE INDEX IF NOT EXISTS ix_nodes_work_status ON nodes(work_id, status);
CREATE INDEX IF NOT EXISTS ix_nodes_wake        ON nodes(wake_kind, wake_at);
CREATE INDEX IF NOT EXISTS ix_nodes_parent      ON nodes(work_id, parent_id);

CREATE TABLE IF NOT EXISTS edges (
    id         TEXT PRIMARY KEY,
    work_id    TEXT NOT NULL,
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    relation   TEXT NOT NULL,
    created_at TEXT,
    payload    TEXT             -- json: relational metadata (e.g. supersedes reason)
);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(work_id, dst, relation);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(work_id, src, relation);

-- append-only source of truth; nodes/edges are the rebuildable projection
CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL,
    ts      TEXT NOT NULL,
    actor   TEXT,
    op      TEXT NOT NULL,
    data    TEXT             -- json: the mutation
);
CREATE INDEX IF NOT EXISTS ix_events_work ON events(work_id, seq);
"""
"""work_objects.model: WorkGraph types + SQLite schema."""
