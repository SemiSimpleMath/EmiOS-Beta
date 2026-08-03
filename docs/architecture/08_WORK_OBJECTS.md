# Work Objects — the execution substrate

Everything actionable in EmiOS is a **work object**: a goal plus a small graph of typed
nodes, event-sourced in SQLite, that agents *mutate* rather than describe. The dayflow
orchestrator plans against it, workers execute inside it, the scheduler wakes from it,
and repair/finalize adjudicate on it. The graph is the source of truth and **the return
channel** — a worker reports by writing nodes, not by passing messages back.

This page is the substrate reference: the model, the store, the invariants, the runtime,
and every writer that touches the graph. For how a *tick* drives it (evaluate → finalize
→ architect → repair → promote → dispatch) see [05_DAYFLOW.md](05_DAYFLOW.md) and
[05a_DAYFLOW_ORCHESTRATOR_REFERENCE.md](05a_DAYFLOW_ORCHESTRATOR_REFERENCE.md). Design
history and the node-taxonomy rationale live in `work_objects/README.md` and
`work_objects/DESIGN.md` (package-local, authoritative on the *why*).

## Where it lives

Top-level package `work_objects/` (a deliberate sibling of `app/`, not inside it — the
dependency is two-way by design: this package imports `app.*`, and the dayflow
orchestrator + worker managers import `work_objects.*`).

| File | Role |
|---|---|
| `model.py` | `WorkObject` / `WorkNode` / `Edge` Pydantic models, derived queries, invariant `validate()`, `SCHEMA_SQL` |
| `store.py` | `WorkStore` — the validated event-sourced writer (`apply()`), transition machine, closure cascade, boot repair |
| `work_runtime.py` | `run_node` / `work_on` — drive one node through a worker manager; result-as-evidence convention |
| `runtime.py` | The work **contextvar** (`set_work_context` / `get_work_context`) binding graph tools to the active node |
| `work_tools.py` | The `work_*` graph tools registered into the live tool registry + `register_manager_as_tool` |
| `tools.py` | `WorkGraphTools` — the underlying op wrappers the tools call |
| `scope.py` | `orchestrator_scope()` — the ceiling scope + stable per-effort identity |
| `ui/blueprint.py` | The `/work` editor (list, graph view, event log, manual node edits) |
| `README.md`, `DESIGN.md`, `EMI_TEAM_VS_WORK.md` | Design docs — node taxonomy, mission tier, worker split |

**Production data lives in `emi.db`** (decision #56): the accessor
`app/assistant/dayflow_orchestrator/work_store.py::get_dayflow_work_store()` opens the
four tables alongside `unified_log_2026`, so the planner's portfolio projection and item
state are transactional joins in one DB. It is a per-path singleton with locked
double-checked creation (audit W3 — two racing first-touches used to mint two stores with
two separate RLocks), runs the one-time `active → dispatched` status migration, and runs
`repair_terminal_zombies()` before any write. `DAYFLOW_WORK_DB` overrides the path
(tests); the `work.db` / `business_run.db` files inside the package serve scenario
harnesses only.

## The model

### WorkObject — the graph container

`status = active | done | abandoned | blocked`, a `goal_node_id`, `constraints` (a JSON
bag for goal-level budget/deadline/values — and `concern_refs`, see below), plus the
in-memory projection (`nodes: dict`, `edges: list`). A future rename of the container
statuses to `open/closed` is noted in the code but not done.

### WorkNode — one unit in the graph

Two tiers, and the discriminator is the architecture rule that keeps the schema stable:

- **Engine tier (first-class columns)** — anything the engine queries, filters, or
  joins: `status`, `parent_id`, `wake_kind/wake_at/wake_ref`,
  `satisfied_when_kind/_ref`, `authority`, `side_effect`, `requires_approval`,
  `deadline`, `pod_ref`, provenance (`created_by`, timestamps).
- **Content tier (LLM-read only)** — `content` (natural language; the node's
  **directive/identity**, never overwritten by results) and `payload` (an open typed
  bag for type-specific facts — a tool's args, `dispatch_epoch`, abandon reasons).

`type` and `status` are **open strings**. The known sets (`NODE_TYPES_KNOWN`:
goal/plan/subtask/tool + evidence/artifact + question + verification;
`NODE_STATUS_KNOWN`) are documentation and switch helpers — a new node type needs zero
schema change; a newly engine-load-bearing field is an additive `ALTER TABLE`. (`notify`
survives as a legacy type only: new graphs mint plain spine nodes and the switchboard
reads each node's *goal* to route it — one node type, handler varies.)

### One tree, one DAG — never both for the same job

- **Ownership is a tree on `parent_id`** — ≤1 parent, roots NULL, acyclic (validated).
  The single spine carries decomposition and the authority ceiling flowing down
  (`add_node` rejects a child authority above its parent's). Edges are never used for
  ownership, so the two representations cannot drift.
- **Dependency/knowledge is a DAG of typed edges** in their own table (`depends_on`,
  `produces`, `verifies`, `answers`, `supports`, `supersedes`, `contradicts`,
  `references`) — relational and indexed because the ready-set query is the hot path.
  A node owned once can be reused by many via edges.

### Derived, never stored

`is_ready(node)` — status ∈ {proposed, waiting, actionable}, `wake_at` not in the
future, and every `depends_on` source satisfied. `is_satisfied(node)` — keyed on
`satisfied_when_kind` (`tool_success`/`user_signoff` → status `closed`;
`all_owned_children_done` → recursive over `parent_id` children; `verified_by` → the ref
node `passed`; default → a terminal-good status). **The satisfied spine terminal is
`closed`, not `done`** — a worker-`done` node has only produced a *result*; the
work_finalizer alone judges it and produces `closed` (commit `cb498a40`). `ready` /
`blocked` / `stale` are computed on demand and never persisted.

## The store — event-sourced, validated, atomic

Four tables: `work_objects`, `nodes`, `edges`, and the append-only `events` log — the
**source of truth**; nodes/edges are the rebuildable projection. Every mutation goes
through one entrypoint:

```python
store.apply(op, data, actor)   # load → validate → rollup → validate() → event + projection
```

Steps commit as **one atomic SQLite transaction** — the event and the projection can
never diverge. All access serializes on one in-process RLock (single-writer by design);
on the shared `emi.db` a `busy_timeout=10000` makes writes wait for the main db_manager
writer instead of failing "database is locked". WAL is on.

**Ops:** `create_work_object` (mints the goal node, `satisfied_when_kind` default
`all_owned_children_done`), `add_node`, `add_edge`, `set_status`, `edit_node` (manual
UI edit — title/content only), `set_work_status` (the steward's authoritative
complete/abandon), `attach_pod`, `defer_node` (set/clear a wake; a `dispatched` node
parks to `waiting`).

**Transition machine.** `FAMILY_BY_TYPE` maps each node type to a lifecycle family
(default `spine`); `TRANSITIONS[family][from] → {allowed targets}` rejects everything
else loudly. The spine: `proposed → actionable → dispatched → done|incomplete →
closed`, side states `waiting`, `failed` (repair re-opens via `proposed` or
re-dispatches), terminal `abandoned`/`superseded`. Knowledge, question, and
verification families have their own lifecycles (`assumed/verified/stale`,
`open/answered/unanswerable`, `active/passed/failed`).

### Invariants and fences

- **Closure-as-transition (431be3a7).** Entering a terminal WorkObject status — via the
  steward's `set_work_status` *or* the automatic `_rollup` when the goal satisfies — is
  a transition with obligations, not a label write: the goal node is mirrored terminal
  and every still-**startable** node (`proposed/actionable/waiting/failed` =
  `_STARTABLE_STATUSES`) is cascade-abandoned with its wake cleared. `validate()`
  enforces the invariant *"a terminal WorkObject contains no startable node"* on every
  apply. In-flight `dispatched` nodes are left to land their result (inert in a terminal
  object). Origin: the 2026-07-30 zombie-wake incident — a `waiting` node inside a
  `done` object kept an armed timer and fired a ghost ticket a day after closure.
  `repair_terminal_zombies()` healed the pre-cascade backlog at boot (182 nodes); a
  nonzero repair count after the first run means some writer bypassed the invariant.
- **Incarnation fence (audit W2).** Every claim (`→ dispatched`) bumps
  `payload.dispatch_epoch`. Completion paths pass `expected_dispatch_epoch`; a zombie
  thread whose node was sweeper-failed and repair-re-dispatched holds a stale epoch and
  its late write is **rejected** — it cannot overwrite the successor incarnation's
  result (`work_runtime` logs "result DISCARDED").
- **Global node ids (slug-theft guard).** `nodes.id` is a global primary key;
  `add_node` refuses a caller-supplied id that already lives in *another* work object
  (INSERT OR REPLACE would silently re-home the row and strand the old graph's
  children/edges). Callers minting meaningful ids namespace them per work object.
- **Rollup is forward-only.** `_rollup` auto-completes the object when the goal
  satisfies (with the same closure cascade); it never reopens a done object, and a
  force-`abandoned` object is never auto-completed.

## Wake primitives

A node parks by carrying a wake condition (`defer_node`): `wake_kind ∈ {time, event,
user_reply, signal}` with `wake_at` (time) or `wake_ref` (event/signal id). Three
consumers act on them:

- **`time`** — the DayflowScheduler's `_arm_work_node_wakes` arms one APScheduler
  one-shot per time-gated node (cap 200); on fire, `_fire_work_node` runs that single
  node through `work_on` iff `is_ready` still holds — precision wakes independent of the
  planning tick (path P7 in 05a).
- **`event` / `signal`** — the state_mover matches incoming intake against parked nodes
  (`work_wait_intake`) and clears the wait with the arrived evidence.
- **`user_reply`** — the ask lane: dispatch parks the node `waiting` with a re-ask
  `wake_at`; the materializer's `_record_replies` matches the ticket reply back by the
  deterministic `trigger_context.work_node` join, appends `[User replied: …]`, and
  clears the wake. Excluded from state_mover promotion (owned by the dispatch ask path).

## The runtime — driving a node

`work_runtime.run_node(store, work_id, node_id, manager_name="work_emi_team_manager")`:

1. **Claim** — flip `proposed/waiting/actionable → dispatched`, snapshot once, capture
   `my_epoch`.
2. **Hand off** — set the work **contextvar** (`runtime.set_work_context`) so the
   `work_*` tools know which node/store they act on, then invoke the worker manager
   through the standard `manager_invoker`. The manager's `node_input` config decides the
   handoff shape: `"task"` (node content as the task + upstream `depends_on` results
   rendered as information — web-manager style) or `"render"` (the manager's render node
   projects the graph; the message still carries the node's real goal so a degraded
   projection can't make the worker invent a task — the 06-23 hallucination fix).
3. **Harvest** — the manager's final answer is recorded as an **evidence child** of the
   node (the node's `content` is its directive — its identity — and is never
   overwritten); a surfaced research pod is attached via `attach_pod`. The node closes
   `done` (clean exit) or `failed` (abort/error) with the epoch fence.
4. The manager's `ToolResult` is returned **verbatim** — calling a node manager is no
   different from calling any manager; status is a graph property.

`work_on(store, work_id, node_id=None)` is the standalone arm: with a node id it runs
that node; with none it drives ready top-level nodes (parent == goal) until the goal
satisfies or only future-wake nodes remain (returns `"parked"` — it never fast-forwards
time). Job-thread dispatch + tick-side supervision (orphaned/frozen, with liveness
inherited up the `parent_id` spine — fixed 2026-08-04) are the dayflow layer:
`node_dispatch.py` and `dispatch_sweeper.sweep_stuck_work_nodes`, documented in 05a §1.

### The worker's graph vocabulary

`register_work_tools(DI.tool_registry)` injects ten `work_*` tools at runtime (nothing
under `app/` — the package stays isolated while fully reusing the tool runtime). Each
reads the active node from the contextvar:

| Tool | Op |
|---|---|
| `work_add_subtask` | decompose: child subtask under my node (+ optional `depends_on`); echoes the running checklist back so the frozen-projection agent doesn't duplicate |
| `work_add_dependency` | my node `depends_on` another |
| `work_record_finding` | mint an Evidence node now |
| `work_produce_artifact` | mint an Artifact node referencing a pod |
| `work_ask_question` | open a Question (`blocks=true` adds the dependency) |
| `work_defer` | park my node with a wake condition |
| `work_finish` | close my node: satisfied / failed / abandoned |
| `work_graph_search` / `work_graph_peek` / `work_graph_summary` | read the graph (peek surfaces content or the pod one-liner, never a bare `datapod:` id) |

`active_attribution_node` nests a planner's tool evidence and delegated child nodes
under the single in-flight checklist subtask it is currently working (goal → checklist
item → delegation), read identically by the reconcile hook and the node handoff.
`register_manager_as_tool` exposes node managers (`work_web_manager`, …) as ordinary
manager-as-tool wrappers at runtime.

### Scope — one stable identity per effort

`scope.orchestrator_scope(work_id=…)` is the ceiling scope for a work effort (authority
99, `allowed_tools: [all]`, and a `per_manager` rule narrowing the coordinator
`work_emi_team_manager` to its ~16-manager delegate roster — exactly like master_room
narrows emi_team). **`scope_id` and `room_id` are both `scope::work_objects::<work_id>`,
shared by every node of the effort** — load-bearing for pods: a pod is minted with the
minter's room_id, and a reader's `allowed_scopes: ["self"]` expands to its room_id, so
an effort's pods are mutually visible across its nodes while distinct efforts stay
isolated. The task runner threads its own run scope instead so an action node executes
at the run's authority, not the substrate's 99 (finding R8).

## Who writes the graph (the tick touchpoints)

| Writer | Ops | When |
|---|---|---|
| **evaluator** (`strategic_planner_wo` via `work_persist`) | `create_work_object`, objective edits, `set_work_status` | converts intake to WOs; authoritative complete/abandon; forwards `constraints.concern_refs` from subconscious-born work |
| **architect** (`work_architect_apply`) | `add_node` (born `proposed`), `add_edge` (`depends_on`), `defer_node` (wake gates), abandon deltas | decomposes new/replanned goals, ≤3/tick |
| **state_mover** | `set_status` (`proposed/waiting → actionable`), wake clears | promotes ready nodes; LLM may only HOLD |
| **dispatch** (`work_node_dispatch_node` / `run_node`) | `set_status` (`→ dispatched`, `→ done/failed`), `defer_node` (asks), `attach_pod`, evidence children | one node per tick + the worker's own subtree |
| **worker** (via `work_*` tools + reconcile hook) | subtasks, evidence, artifacts, questions, defers | inside the job thread |
| **finalizer** (`work_finalizer_node`) | `set_status` (`done → closed`), `set_work_status` (resolve) | SOLE producer of `closed`; propagates concern outcomes |
| **repair** (`work_repair_apply`) | `failed → proposed` (retry/escalate + `defer_node`), `set_work_status abandoned` | adjudicates failed nodes; user decline is authoritative |
| **sweeper** (`sweep_stuck_work_nodes`) | `set_status` (`→ failed`) | orphaned/frozen jobs (ancestor-liveness aware) |
| **/work UI** | `edit_node`, `set_status`, `set_work_status`, node add/remove | owner's manual surface |

Concern back-propagation: a WO carrying `constraints.concern_refs` reports its terminal
outcome to the subconscious register (`concern_feedback.propagate_work_outcome`,
ad887863) from the finalizer and repair paths.

## The /work UI

`work_objects/ui/blueprint.py` (`work_ui_bp`, registered in `create_app`): `/work` list
+ graph view, `/api/work/<id>` (graph JSON), `/api/work/<id>/events` (the event log —
the audit trail per object), `/api/work-pod` (pod summary), and manual mutators
(abandon, node status/edit/add/remove) that go through the same validated `apply()` as
every other writer.

## Deferred by design

- **Mission tier** — the open-ended, never-done container (goals/experiments/metrics on
  a review cadence) that spawns/retires WorkObjects. v1 is WorkObject-only.
- **Delegation by reference** — a node spawning a *separate* child WorkObject
  (`parent_work_id`/`parent_node_id`, `satisfied_when_kind="child_work_done"`) instead
  of in-place `parent_id` expansion. Designed in README §Nesting; not built.
- **Per-node budget** — rides the same `parent_id` ceiling channel as authority when it
  lands; v1 enforces at the root via `WorkObject.constraints`.
- **Container status rename** `active/done → open/closed`.
- **`quality_bar`** satisfied-when kind (falls back to plain terminal-good statuses).

## Cross-references

- [05_DAYFLOW.md](05_DAYFLOW.md) — the tick pipeline that plans against this substrate.
- [05a_DAYFLOW_ORCHESTRATOR_REFERENCE.md](05a_DAYFLOW_ORCHESTRATOR_REFERENCE.md) — per-agent detail, paths P3–P8, supervision.
- `work_objects/README.md` / `DESIGN.md` — node taxonomy rationale, mission tier, worker-split design.
- [14_PODS.md](14_PODS.md) — the pod scope wall the shared effort identity exists for.
- [15_EMI_TEAM_AND_SCOPE.md](15_EMI_TEAM_AND_SCOPE.md) — the scope model `orchestrator_scope` participates in.
