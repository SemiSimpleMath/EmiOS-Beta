# Work Objects — the execution substrate

## Main tasks and worker provenance

Only a `subtask` whose `parent_id` is the work object's goal is an orchestrator work
unit (`WorkObject.is_work_unit`). Worker-created descendants are **provenance records**
of execution within an assigned task: internal checklist state, delegated attempts,
failures, findings and saved outputs. They share the existing graph storage types;
that does not make them independently schedulable graph tasks. No schema migration
or rewriting of existing records is required for this classification.

- Promotion, materialization, timed/event wakes, dispatch/session entry, boot ask
  reconnection and inactivity supervision exclude provenance as independent assignments.
  Supervision still uses activity anywhere in the owned history to assess the main task.
- A replacement worker sees the complete owned history, including nested results and
  failures, so it can reuse prior work. Unfinished/dispatched provenance records describe
  the last recorded attempt, not proof of a currently running independent job.
- The finalizer reads the full main-task directive, its returned result and full owned
  provenance. Its outcome summarizes what was tried, went wrong, succeeded and remains
  unresolved, preserving useful saved-output references.
- Architect/steward views show main tasks and finalizer summaries, not the worker's
  internal record list or raw results. Missing summary is labeled awaiting finalizer
  summary. Architect deltas cannot directly target or depend on provenance records.
- Internal helper failures do not increment the whole goal's failed-attempt count.
  Existing historical counts are preserved; this change does not repair past data.
- `/work` labels these records provenance and excludes them from schedulable-task counts
  and ready/blocked badges. The owner can still inspect the execution history.

This boundary does not solve the separate claim/epoch/finalizer-recovery defects in
LIVE 3, WO1 and DF4. Generic graph `is_ready` remains a local dependency/time predicate;
orchestrator callers must also enforce `is_work_unit`.


Everything actionable in EmiOS is a **work object**: a goal plus a small graph of typed
nodes persisted in SQLite, that agents *mutate* rather than describe. The dayflow
orchestrator plans against it, workers execute inside it, the scheduler wakes from it,
and the finalizer adjudicates on it. The graph holds durable state; workers also return
ToolResults, which the recorder translates into graph writes before finalization.

This page is the substrate reference: the model, the store, the invariants, the runtime,
and the main Dayflow writers that touch the graph. For how a *tick* drives it (evaluate → architect
→ promote → dispatch, with the finalizer judging the result in the dispatch room) see
[05_DAYFLOW.md](05_DAYFLOW.md) and
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
| `store.py` | `WorkStore` — the validated graph writer with an input audit log (`apply()`), transition machine, closure cascade, boot repair |
| `discharge.py` | `discharge_node` / `drive_work` — drive one node through a worker manager; scope REQUIRED (caller-derived), session stamp after claim, result-as-evidence convention |
| `runtime.py` | The work **contextvar** (`set_work_context` / `get_work_context`) binding graph tools to the active node |
| `runtime_setup.py` | `ensure_manager_services()` loads `.env`, supplies missing `mam_instance_manager`, and preloads configs. Written for minimal test bootstrap, but also called by `discharge._ensure_registered`; not a test-only execution path |
| `result_recorder.py` | Shared tool-result recorder. Preserves the directive, optionally attaches a pod, writes `done`/`failed`, then adds evidence. These are separate transactions; only the status write has the optional epoch check. Error type, `aborted`, `exit_state=error_exit`, or empty answer means failure. The finalizer judges meaning afterwards |
| `work_tools.py` | The ten `work_*` tools, built from one `_SPECS` table and injected straight into `tool_registry.registry` at runtime (synthesised contracts: domain `work_graph`, `min_authority` 0); also `pod_summary` and the unused `register_manager_as_tool` |
| `tools.py` | `WorkGraphTools` — plain-Python op wrappers bound to ONE node, which the registered tools call, so a scripted agent can drive the graph with no LLM. Holds one read method the tool layer never exposes: `graph_neighbors` |
| `scenarios/_scenario_scope.py` | DEV-ONLY harness scope — production authority always derives from the caller (room / task run) |
| `ui/blueprint.py` | The `/work` editor (list, graph view, event log, manual node edits) |
| `README.md`, `DESIGN.md`, `EMI_TEAM_VS_WORK.md` | Design docs — node taxonomy, mission tier, worker split |

**Production data lives in `emi.db`** (decision #56): the accessor
`app/assistant/dayflow_orchestrator/work_store.py::get_dayflow_work_store()` opens the
five core tables alongside `unified_log_2026`, allowing joins in one DB. Separate
work/item writes do not thereby share a transaction; migration metadata adds `work_store_meta`. It is a per-path singleton with locked
double-checked creation (audit W3 — two racing first-touches used to mint two stores with
two separate RLocks), runs **two** one-time migrations, and runs `repair_terminal_zombies()`
before returning a newly created singleton. Migration failures are logged and swallowed;
obtaining the store does not prove both migrations succeeded. `DAYFLOW_WORK_DB` overrides the path (tests); the `work.db` /
`business_run.db` files inside the package serve scenario harnesses only.

The two migrations are worth knowing because they encode two model changes:

- **`active → dispatched`** — the in-flight rename. Verification nodes keep `active` (a
  verification run genuinely in progress). Plain `UPDATE`, idempotent, so it self-guards.
- **`waiting → dispatched` for surfaced asks** (2026-08-18 owner ruling: a surfaced ask IS
  an in-flight tool call) — also clears `wake_at`, since the re-ask timer is retired. This
  one is guarded by a marker row in a `work_store_meta` table because it must run **exactly
  once**: under the current model `waiting` + `wake_kind=user_reply` legitimately reappears
  for a *pre-surface* ask parked by a state_mover HOLD, and a second run would mis-flip
  those to in-flight.

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
- **Content and payload** — `content` is normally the directive, preserved by result
  recording but editable through the store. `payload` contains both agent-facing facts
  and machine-read state: `dispatch_epoch`, finalizer verdicts, counters and session IDs.

`type` and `status` are **open strings**. The known sets (`NODE_TYPES_KNOWN`:
goal/plan/subtask/tool + evidence/artifact + question + verification;
`NODE_STATUS_KNOWN`) are documentation and switch helpers — a new node type needs zero
schema change; a newly engine-load-bearing field is an additive `ALTER TABLE`. (`notify`
survives as a legacy type only: new graphs mint plain spine nodes and the switchboard
reads each node's *goal* to route it — one node type, handler varies.)

### The status vocabulary (audited 2026-09-16)

Every status word has to be taught to the agents that read or write it, so the vocabulary is a cost,
not a free label space. **One glossary — `work_portfolio.STATUS_LEGEND` — is the single source**, and
every reader takes it from there: the steward and the dayflow situation snapshot through
`render_portfolio`, which prepends it; the finalizer by prepending it to the single object's
projection; the state_mover *and the wake lane* as a `node_status_legend` blackboard item their prep
nodes set. Do not write a second one in a prompt. (The retired `work_repair_node` renders it too;
that path is unwired.)

| status | meaning |
|---|---|
| `proposed` | planned, in the architect's inbox; not yet approved to run |
| `actionable` | approved and QUEUED — one node dispatches per tick; queued is NOT stalled |
| `dispatched` | IN-FLIGHT — a worker is on it, or an ask is out. Do not re-dispatch |
| `waiting` | held ON PURPOSE on a time / event / dependency gate; held is NOT stalled |
| `done` | produced a RESULT; the finalizer has not judged it yet |
| `closed` | judged and counted — the satisfied terminal, the only one that completes a goal |
| `failed` | the tool errored/returned nothing, the finalizer judged NOT ACHIEVED, or dispatch/session/sweeper failure ended the call. A finalizer judgment carries `outcome`, `recommendation`, and a route; direct failures may have no verdict. Every not-achieved verdict passes through here, including a call that returned cleanly |
| `abandoned` | dropped |
| `superseded` | replaced by newer work |

**`done` is not "finished".** A top-level node counts toward its goal only once the finalizer closes
it. (The glossary taught `done: finished.` until 2026-09-16 and omitted `closed` entirely — while
`closed` rendered verbatim in the steward's own prompt.) Note the one asymmetry `is_satisfied`
enforces: a worker's own checklist child (parent != goal) is satisfied at `done`, because the
finalizer only judges top-level nodes.

**The rule that keeps this small: status = lifecycle position (who may act next); the RESULT text =
what happened.** When an outcome nuance wants to become a status — "acted on but couldn't", "expired,
not reached" — that is the signal it belongs in the node's result evidence instead, which the
finalizer already reads in full and which needs no glossary.

**Additional supported vocabulary:** knowledge `verified`/`stale`, question
`answered`/`unanswerable`, and verification `active`/`passed` remain in the model
and transition tables. This persistence audit does not certify all their producers.
`active` is also a normal WorkObject container status. `stale` is stored, not derived.

### Ownership tree and relationship edges

- **Ownership is a tree on `parent_id`** — ≤1 parent, roots NULL, acyclic (validated).
  It carries decomposition. `add_node` compares authority only when both child and
  immediate parent have explicit values; it does not resolve ancestor inheritance. Edges are never used for
  ownership, so the two representations cannot drift.
- **Dependency/knowledge uses typed edges** in their own table (`depends_on`,
  `produces`, `verifies`, `answers`, `supports`, `supersedes`, `contradicts`,
  `references`) — relational and indexed because the ready-set query is the hot path.
  A node owned once can be reused by many via edges. Validation checks endpoints,
  not dependency cycles, self-links or duplicate relationships. A DAG remains the intent.

### ActionRecord — what this goal actually DID to the outside world

A fourth model, with its own table and its own op (`record_action`). Nodes record what is
*planned* and what state it is in; nothing recorded what was **done**, so no planning pass
could see its own past tense. Origin: a stuck goal emailed one recipient eleven times in
fifty-two minutes while every guard in the system counted timeouts, prunes and minutes —
because nothing counted sends.

Fields: `channel` (email | ticket | sms | chat | post | call), `target`, `summary`,
`outcome` (sent | expired | answered | dismissed | failed), `actor`, `ts`, optional
`node_id`, `payload`.

Two deliberate properties:

- **Keyed on `work_id`, not `node_id`** — so the ledger survives the replanning that
  resets every node-keyed counter.
- **Written by the TOOL that causes the side effect, at the moment it happens**, and
  deliberately unvalidated against node state: an act that reached the outside world is a
  fact, and a ledger that can refuse a fact is worse than none. An `outcome` is revised by
  appending a new row, never by editing one.

`work_portfolio` renders this as the ACTIONS TAKEN block, and calls out repeats to one
target — the projection every planning pass reads before deciding to act again.

### Derived, never stored

`is_ready(node)` — status ∈ {proposed, waiting, actionable}, `wake_at` not in the
future, and every `depends_on` source satisfied. `is_satisfied(node)` — keyed on
`satisfied_when_kind` (`tool_success`/`user_signoff` → status `closed`;
`all_owned_children_done` → nonempty set of ALL `parent_id` children recursively; `verified_by` → the ref
node `passed`; default → a terminal-good status). **The satisfied spine terminal is
`closed`, not `done`** — a worker-`done` node has only produced a *result*; the
normal Dayflow finalizer judges it and produces `closed`. The store does not reserve
that status to the finalizer. Nested `subtask` nodes at `done` satisfy before contract
checks. `ready` and node `blocked` are queries; knowledge `stale` and container `blocked`
are stored statuses. `is_ready` does not check `wake_kind`, `wake_ref`, container status,
authority or approval; event/signal matching belongs to the higher-level wake machinery.

## The store — graph state, audit events and transactions

Five core tables hold containers, nodes, edges, actions and events. The graph tables
are current state. `events` records the original mutation inputs, omitting generated
node/edge/action IDs and implicit cascade details. There is no replay implementation;
the older promise of a rebuildable projection is a deferred gap.

```python
store.apply(op, data, actor)   # load → handler → rollup → validate → event + graph
```

Each `apply` acquires `BEGIN IMMEDIATE` before loading the graph, then commits its
mutation event and graph writes together. SQLite serializes separate connections;
the instance RLock protects its connection. Multiple calls remain separate transactions.
Boot migrations and closure repair write outside `apply`.

The writer includes `claim_task`, `record_result`, `finalize_task`, `bind_ticket`,
`batch`, `redirect_dependencies` and `revise_goal`, alongside the basic node/edge,
status, pod, wake, action and instruction operations. Architect revisions commit
node/edge/wake/prune changes and exact-instruction acknowledgment in one batch with
a snapshot-version fence. New goal source details are included in its creation write.

**Transition machine.** `FAMILY_BY_TYPE` maps each node type to a lifecycle family
(default `spine`); `TRANSITIONS[family][from] → {allowed targets}` rejects everything
else loudly. The spine: `proposed → actionable → dispatched → done → closed`, side states
`waiting` and `failed` (the finalizer's `retry` re-opens it to `proposed`; it can also be
re-dispatched or abandoned), terminal `abandoned`/`superseded`. `done → failed` exists so
a call that RETURNED but achieved nothing can still reach the architect. Knowledge,
question, and verification families have their own lifecycles (`assumed/verified/stale`,
`open/answered/unanswerable`, `active/passed/failed`).

One gap worth knowing: the transition check runs **only when the target differs from the
current status**, so a same-status write (e.g. `dispatched → dispatched`) bypasses
the transition lookup. Epoch and actor-specific checks still run. `add_node` accepts
initial statuses only within the node family's lifecycle; terminal reasons on initial rows remain caller-owned metadata. `defer_node` changes
`dispatched` to `waiting` directly except for `wake_kind=user_reply`; it checks wake-kind
vocabulary and validates any supplied wake timestamp (naive means UTC), and requires a time wake timestamp or external event/signal reference. `actor` and `licensed`
are caller-supplied data, not authenticated permissions.

### Invariants and fences

- **Closure cascade.** Entering a terminal WorkObject status — via the
  steward's `set_work_status` *or* the automatic `_rollup` when the goal satisfies — is
  a transition with obligations: explicit closure mirrors a nonterminal goal, while
  auto-rollup marks the satisfied goal `done`. Every still-**startable** node (`proposed/actionable/waiting/failed` =
  `_STARTABLE_STATUSES`) is cascade-abandoned with its wake cleared. `validate()`
  enforces the invariant *"a terminal WorkObject contains no startable node"* on every
  apply. In-flight `dispatched` nodes are left to land their result (inert in a terminal
  object). Origin: the 2026-07-30 zombie-wake incident — a `waiting` node inside a
  `done` object kept an armed timer and fired a ghost ticket a day after closure.
  `repair_terminal_zombies()` healed the pre-cascade backlog at boot (182 nodes); a
  nonzero repair count after the first run means some writer bypassed the invariant.
  One exception inside the exception: a `dispatched` **ask** (`wake_kind=user_reply`) has
  a waiting tool call, but closure moots the question and it cascades too. Closure
  does not cancel that thread or its ticket; the recorder skips its later result.
  Boot repair selects terminal objects with startable nodes, so an object containing
  only a dispatched ask is not selected by that repair query. The same subtree cascade fires when any node
  reaches `done`/`closed`/`abandoned`/`superseded`: its unstarted descendants stop with
  it, because a node that has declared its outcome cannot have it changed by children that
  never began (the 2026-09-13 runaway — a closed research node whose descendants kept
  spawning ten levels down while every supervisor read "progress: 1/2").
- **Incarnation fence.** `claim_task` exclusively claims an actionable, currently
  ready main task and advances `dispatch_epoch`. A same-status `set_status` is not
  a claim. `record_result` checks the epoch, attaches evidence/pod data and changes
  status in one transaction; stale and duplicate receipts are rejected. Timeout
  writes additionally recheck subtree inactivity in that transaction. `finalize_task`
  accepts one judgment per attempt and commits its status, summary and counters together.
- **Reason gates.** Changed `set_status` targets `closed`/`abandoned`/`superseded`
  require a nonempty reason, saved as `payload.terminal`. Terminal `set_work_status`
  also requires one. This is not a rule for every terminal-classified status or for
  initial node creation. The reason gate preserves WHY for later planning (origin:
  187 reasonless abandons driving a replan loop).
- **The churn fence.** Abandoning a node that is `actionable`, or `waiting` with a FUTURE
  wake, requires `licensed` in the write. Queued and held work belongs to the runtime and
  WILL run — "hasn't run yet" is not "will never run". 33 generations of one delivery node
  died to replan churn before this existed. A licence means evidence: the finalizer's
  verdict on that node, or a user directive.
- **The failed-node fence.** A `set_status` on a `failed` node by `actor == "architect"`
  without `licensed` is refused. The finalizer's verdict is what licenses what happens next
  (retry / new approach / stop / ask the user). Origin: the architect's prompt once claimed
  failed nodes "come back to you", so it consumed each failure as a planning problem —
  abandon, mint a fresh identical ask, hourly, nine times.
- **The in-flight-ask fence.** A `dispatched` node with `wake_kind=user_reply` cannot be
  changed to `closed`/`abandoned`/`superseded` through `set_status`: the reply is
  its result. `done`/`failed` and closure cascades remain possible.
- **Global node ids (slug-theft guard).** `nodes.id` is a global primary key;
  `add_node` refuses a caller-supplied id that already lives in *another* work object
  (INSERT OR REPLACE would silently re-home the row and strand the old graph's
  children/edges). Callers minting meaningful ids namespace them per work object.
- **Rollup is forward-only.** `_rollup` auto-completes the object when the goal
  satisfies (with the same closure cascade); it never reopens a done object, and a
  force-`abandoned` object is never auto-completed. On completion it writes the goal's
  epitaph naming each child and its finalizer verdict — **after** the cascade, which would
  otherwise overwrite it — so a hollow completion (every child judged not-achieved) is
  legible to the next planning pass. The satisfied goal is marked `done` before the cascade, including when it was proposed or waiting.
- **Auto-completion yields to a pending plan change.** If any node carries an unconsumed
  finalizer verdict with a `next_step`, `_rollup` **holds** the object open rather than
  completing it: the architect that acts on that verdict runs a LATER tick, and a
  completion would destroy the recommendation before its reader exists (the reader scans
  ACTIVE objects only). The steward's explicit `set_work_status` still wins — a person
  deciding a goal is over outranks a pending note about how to continue it.
- **Finalizer-owned failure counts.** A `retry` or `unrecoverable` judgment increments
  the main task's `failure_count` and the goal's `goal_unmet_attempts`, once per
  dispatch epoch. Tool errors, timeout status, and internal provenance failures do
  not themselves increment either count. The goal count survives task replacement.


## Wake primitives

A node parks by carrying a wake condition (`defer_node`): `wake_kind ∈ {time, event,
user_reply, signal}` with `wake_at` (time) or `wake_ref` (event/signal id). Three
consumers act on them:

- **`time`** — the DayflowScheduler's `_arm_work_node_wakes` arms one APScheduler
  one-shot per time-gated node (cap 200, soonest first); on fire, `_fire_work_node` opens
  **`dayflow_wake_manager`** for that single node — a manager with no planning stage in its
  state_map, so a wake cannot re-plan. It holds the scheduler's `_run_gate` (shared with
  the planning tick, so one pass runs at a time) and re-checks `is_ready` **inside** the
  gate, so a wake queued behind another pass sees that pass's writes.
- **`event` / `signal`** — the state_mover matches incoming intake against parked nodes
  (`work_wait_intake`) and clears the wait with the arrived evidence.
- **`user_reply`** — the ask lane. An in-flight ask is a TOOL CALL whose result is the
  user's reply: the node stays **`dispatched`** (`defer_node` deliberately does not park a
  `user_reply` wake to `waiting`), and **there is no re-ask timer**. It ends one of three
  ways — the reply/dismissal returns a result, ticket expiry returns "user not reached", or the
  whole object closes and the cascade clears it. Returned results are recorded and finalized;
  the sweeper records a failed timeout result and cadence resumes its finalizer. The store refuses a
  change to `closed`/`abandoned`/`superseded` on one while the question is out. A *pre-surface* ask (parked before it was ever shown) does
  ride state_mover promotion toward its first surface.

## The runtime — driving a node

`discharge.discharge_node(store, work_id, node_id, *, scope_context, manager_name,
session_id=None)` — **scope_context is required and always caller-derived** (the dayflow
WorkSession passes the orchestrator room's scope, the task runner its run scope,
scenarios their declared scope; a missing scope raises):

1. **Verify the claim — it does NOT claim.** `work_node_dispatch_node` marks the node
   `dispatched` *before* any tool is called, so by the time this runs the node is already
   in flight and every other consumer sees that. `discharge_node` **raises** if the node
   is any other status ("the dispatch gate claims a node before any tool is called") —
   a node that is somehow unclaimed is a dispatch bug, made loud rather than silently
   re-claimed. It only stamps `payload.session_id` when one is passed and differs
   (ownership is a graph fact; children grown under the node inherit the stamp at
   creation, store-level), takes one post-claim snapshot, and captures `my_epoch`.
2. **Hand off** — set the work **contextvar** so the `work_*` tools know which
   node/store they act on, then invoke the worker through **`ManagerInterface.invoke_on`**
   — the same manager-as-tool call every other caller uses. This matters: the work lane
   used to hand-roll `create_manager` + `manager_invoker.invoke` with a message of its own
   shape, which made it the one caller in the system that skipped
   `ScopeAdapter.for_sub_manager` (the single seam where a sub-manager's scope is built)
   and turned a manager failure into a raised exception instead of the structured tool
   error everyone else receives. The manager's `node_input` config decides the handoff
   shape: `"task"` (node content + upstream `depends_on` results) or `"render"` (graph
   projection; the message still carries the node's real goal).
3. **Harvest — one choke point.** `result_recorder.record_tool_result` turns the result
   into graph state, passing `my_epoch` to its atomic result transaction: the
   manager's final answer lands as an **evidence child** titled "manager result" (the
   node's `content` is its directive and is never overwritten). What the result MEANS is
   the finalizer's job, not this layer's. Recording used to be inlined here and was
   skipped entirely whenever the worker had already set its own status — so the caller
   recorded nothing at all, not even the evidence.

`drive_work(store, work_id, *, scope_context, node_id=None)` is a legacy scenario
helper. An explicit node must already be `dispatched`. The automatic branch selects
ready nodes without claiming them, so `discharge_node` rejects its first candidate.
Neither branch invokes the finalizer. It is not currently a working run-to-goal driver.

**Dayflow dispatch is the WorkSession** (`dayflow_orchestrator/work_session.py`): a copy
of the orchestrator room, open until its call returns. `open_session` does **not** branch on
ticket-vs-work and does not claim — the dispatch gate already claimed the node, and it *refuses* one
that is not already `dispatched`. It registers the session first, stamps `session_id` on the graph,
and starts a thread that opens `dayflow_dispatch_manager` on that node under the ROOM'S OWN scope
(`room_session_scope` — the standard loader over `rooms/dayflow_orchestrator/scope.yaml`: pods
`[all]`, authority 95, write_kg). Both branches run the same three stages; only the tool the
switchboard named differs. The ticket branch used to be the exception — it surfaced a ticket by hand,
parked the node on a `user_reply` wake and returned nothing, so the user's answer had to be
reconstructed from the ticket store later.

Supervision (`sweep_stuck_work_nodes`) reads the graph and applies **one rule**: nothing written to
this node or its owned subtree for `ASK_WINDOW_HOURS * 3600 + 20 min` (80 minutes today) → `failed`.
It consults **no** session liveness — a run blocked in a tool call writes nothing meanwhile, so a
dead process and a wedged call are indistinguishable and want the same remedy — and the tolerance is
derived from the ask window so a question the user has not answered yet is never failed out from
under them. The session-ownership supervision this replaced left plumbing behind:
`payload.session_id` is still written in three places and read by nothing, and
`dispatch_sweeper._session_root_dispatched` has no callers. `session_alive_by_id` is the one live
piece, used by `re_arm_inflight_asks()` at boot — which reconnects an ask that outlived its process
from the ticket side, landing an answer already given or waiting out the remainder of the window on
the ticket already on screen, rather than asking twice.

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
| `work_ask_question` | open a graph Question (`blocks=true` adds a dependency); does not create a user ticket |
| `work_defer` | park my node with a wake condition |
| `work_finish` | **state a verdict — it does NOT close the node.** `satisfied`/`failed`/`abandoned` is recorded as an evidence finding (`WORKER VERDICT (…)`) and the worker is told to return control; the finalizer judges it with everything else the node produced. It used to write the terminal status directly, which made the worker the one caller able to pre-empt its own dispatcher: the node was already terminal when the manager returned, so the recorder wrote nothing — not the status, not the epoch fence, not the result evidence |
| `work_graph_search` / `work_graph_peek` / `work_graph_summary` | read the graph (peek returns selected node fields plus content/pod summary; summary lookup can fall back to the bare pod id) |

Every one of them catches its own exceptions and returns the error as ordinary tool
*content* ("ERROR from `work_add_subtask`: … Fix your arguments and try again") rather
than raising — a malformed call is recoverable by the agent and must not kill the manager
loop.

`active_attribution_node` nests a planner's tool evidence and delegated child nodes under
the single in-flight (`dispatched`) checklist subtask it is currently working — goal →
checklist item → delegation. It is read identically by `WorkPlanner._reconcile_to_graph`
and the `manager_interface` node handoff, so every `work_*` manager attributes the same
way; ambiguity (0 or >1 in-flight children) falls back to the owned node.

`register_manager_as_tool`, in the same module, would register a manager-as-tool wrapper
at runtime — but **nothing calls it**. The two node managers ship as ordinary *static*
tool directories (`app/assistant/lib/tools/work_web_manager/`,
`…/work_emi_team_manager/`) like every other manager-as-tool. Treat the function as unused.

### Scope — one stable identity per effort

Authority is never minted below the room. Workers run under the **dayflow room's
scope** loaded through the standard room loader; the task runner threads its run scope
(R8 semantics); the node-handoff (`manager_interface`) passes the calling message's
already-narrowed scope. Worker-minted pods are **room-scoped** (mutually visible across
an effort's nodes via the room; master_room sees them through the system-scope
equivalence) and carry `{work_id, node_id}` in pod **metadata** — the effort join is
data, not scope. The room's `scope.yaml` also carries the `work_emi_team_manager`
narrower-cost roster (~16 tools). Scenario harnesses, the only standalone consumers,
declare a DEV-ONLY `scenario_scope()` under `scenarios/`.

## Who writes the graph (the tick touchpoints)

| Writer | Ops | When |
|---|---|---|
| **evaluator** (`strategic_planner_wo` via `work_persist`) | `create_work_object`, objective edits, `set_work_status` | converts intake to WOs; authoritative complete/abandon; forwards `constraints.concern_refs` from subconscious-born work |
| **architect** (`work_architect_apply`) | `add_node` (born `proposed`), `add_edge` (`depends_on`), `defer_node` (wake gates), abandon deltas | decomposes new/replanned goals, ≤3/tick |
| **state_mover** | `set_status` (`proposed/waiting → actionable`), wake clears | promotes ready main tasks only; LLM may HOLD; provenance is excluded |
| **dispatch** (`work_node_dispatch_node` -> `work_session.open_session` / `discharge_node`) | `set_status` (`→ dispatched`, `→ done/failed`), `defer_node` (asks), `attach_pod`, evidence children | one node per tick + the worker's own subtree |
| **worker** (via `work_*` tools + reconcile hook) | subtasks, evidence, artifacts, questions, defers | inside the job thread |
| **finalizer** (`work_finalizer_node`) | `set_status` (`done → closed`; `→ failed`; `failed → proposed` on retry), `payload.finalizer` (verdict + outcome + route) | normal Dayflow producer of `closed`; not a store-level exclusivity rule; escalates repeated failure to `ask_user` |
| **repair** (`work_repair_apply`) | — | RETIRED 2026-09-16; files remain, unwired. Judged failures carry the finalizer's route (retry / stop / new_approach / ask_user); abandoning a goal is the steward's |
| **sweeper** (`sweep_stuck_work_nodes`) | `set_status` (`→ failed`) | dispatched main tasks with over 80 minutes of owned-history inactivity; no session-liveness check or finalizer invocation |
| **/work UI** | `edit_node`, `set_status`, `set_work_status`, node add/remove | owner's manual surface |

Concern back-propagation: a WO carrying `constraints.concern_refs` reports its terminal
outcome to the subconscious register (`concern_feedback.propagate_work_outcome`). It fires from the **steward's** `set_work_status` only — `work_persist` calls it
directly on complete and abandon. The one other call site is the retired `work_repair_apply`.
**Not** the finalizer: that path needs a terminal *WorkObject* status, and the finalizer
stopped setting one when `resolve` was removed, so a concern is reported on when the steward
ends the goal rather than when the finalizer closes its last node. The `concern:` refs
themselves are lifted out of the evaluator's `based_on` at creation, in `work_persist`.

## The /work UI

`work_objects/ui/blueprint.py` (`work_ui_bp`, registered in `create_app`): `/work` list
+ graph view, `/api/work/<id>` (graph JSON), `/api/work/<id>/events` (the event log —
the audit trail per object), `/api/work-pod` (pod summary), and manual mutators
(abandon, node status/edit/add/remove) that go through the same validated `apply()` as
other apply callers. Object abandonment records the reason "owner abandoned via /work". `_get_store` falls back to `WORK_DB` on any live-store exception,
which can hide a production-store failure; `/api/work` reports the actual `store.path`.
Closing graph state does not cancel active threads or tickets.

## Deferred by design

- **Mission tier** — the open-ended, never-done container (goals/experiments/metrics on
  a review cadence) that spawns/retires WorkObjects. v1 is WorkObject-only.
- **Delegation by reference** — a node spawning a *separate* child WorkObject
  (`parent_work_id`/`parent_node_id`, `satisfied_when_kind="child_work_done"`) instead
  of in-place `parent_id` expansion. Designed in README §Nesting; not built.
- **Per-node budget** — rides the same `parent_id` ceiling channel as authority when it
  lands; `WorkObject.constraints` currently stores metadata without WorkStore budget
  enforcement. Approval, side-effect and deadline fields are likewise not authorization
  gates in the graph writer; callers need the normal execution-scope checks.
- **Container status rename** `active/done → open/closed`.
- **`quality_bar`** satisfied-when kind (falls back to plain terminal-good statuses).

## Cross-references

- [05_DAYFLOW.md](05_DAYFLOW.md) — the tick pipeline that plans against this substrate.
- [05a_DAYFLOW_ORCHESTRATOR_REFERENCE.md](05a_DAYFLOW_ORCHESTRATOR_REFERENCE.md) — per-agent detail and supervision.
- `work_objects/README.md` / `DESIGN.md` — node taxonomy rationale, mission tier, worker-split design.
- [14_PODS.md](14_PODS.md) — the pod scope wall the shared effort identity exists for.
- [15_EMI_TEAM_AND_SCOPE.MD](15_EMI_TEAM_AND_SCOPE.md) — the scope model the room-derived session scope participates in.
