# work_objects — the WorkObject substrate (live)

> **Status: LIVE — the dayflow orchestrator's execution substrate**, in the main repo since 2026-06-21.
> The production work store lives in **emi.db** (four additive tables, opened via
> `app/assistant/dayflow_orchestrator/work_store.py`); the `work.db`/`business_run.db` files here serve
> the scenario harnesses only. Since the cutover the dependency is two-way by design: code here imports
> `app.*`, and the dayflow orchestrator + the worker managers import `work_objects.*`.
> The **data model below (§node taxonomy, principles) is authoritative.** For execution, the worker
> inner loop shipped per `DESIGN.md` (as `work_emi_team_manager`); the outer loop is the dayflow
> pipeline — evaluate → finalize → architect → repair → promote → dispatch — documented in
> `docs/architecture/05_DAYFLOW.md`.

## Why this exists
A new execution substrate for **long-running, open-ended work** — "improve my life", "run a business", multi-day projects — distinct from the Message-native dayflow/manager stack. Work is a durable, typed **graph that agents mutate**, not flat text/Messages they pass around. The graph is the source of truth; the transcript is just an event log.

## The two top objects
- **WorkObject** — a *bounded, terminal* unit of work: an event-sourced WorkGraph (DAG of typed nodes) with a Goal + a contract; reaches `done`/`abandoned`.
- **MissionObject** — *open-ended, never done*: maintains goals, evidence, experiments, routines, metrics, constraints, decisions; spawns/retires WorkObjects; **reviewed on a cadence** (no `satisfied_when`). Its engine is the **Experiment** (hypothesis → intervention(WorkObjects) → metric → decision → routine/revise). Most registers are references into existing stores (beliefs/KG = evidence, routine_manager = routines, telemetry+insights = metrics, scope = constraints, subconscious = the initiative generator). **Deferred to a later phase — v1 is WorkObject-only.**

## Nesting & delegation
A WorkObject is **flat**: its children are always `WorkNode`s — one `work_id`, one event log, one atomic-write unit — **never** WorkObjects. Work still nests, but by **delegation by reference, not containment**: a node spawns a *separate* child WorkObject (its own `id`/goal/lifecycle/log) and references it; the child links up via `parent_work_id`/`parent_node_id`. This is the live `Orchestrator → sub-orchestrator` pattern (`request_handler(depth+1)`) and `Mission → WorkObject`, one level down. Inlining a WorkObject into a node would wreck the flat projection, the one-log-per-object truth, and the per-object atomic transaction — reference keeps all three and adds reuse + independent cadence + scope boundaries.

- **Two decomposition modes, the node owner's call at the boundary:** expand **in-place** (`parent_id` children, same graph) for cohesive sub-work that shares the goal's contract/budget/log and finishes as part of finishing the goal; **spawn a child WorkObject** when the sub-work is independently bounded-and-terminal, runs on its own cadence, may be reused, or crosses a scope/owner boundary.
- **Almost no new schema:** a delegation node is `satisfied_when_kind = "child_work_done"` (open vocab) + the child `work_id` in `payload`; the only additive columns are `work_objects.parent_work_id` / `parent_node_id` (the cross-object tree). Completion propagates **up explicitly** when the child hits `done` (one `set_status` on the parent node) — *not* a reactive invalidation daemon. Acyclicity + a depth cap live in the orchestrator.
- **Deferred** (like per-node budget): v1 is a single flat WorkObject; delegation is the first step past the proving ground and the bridge to the Mission tier.

## Node taxonomy (WorkObject)
Work spine: **Goal · Plan · Subtask · Tool**  |  Knowledge: **Evidence · Artifact**  |  Open loop: **Question**  |  Check: **Verification**.

- **Shared base (engine tier, validated — first-class columns):** `id, type, status, parent_id, satisfied_when, owner_agent, authority, requires_approval, side_effect{read|mutate|irreversible}, wake_on{time|event|user_reply|signal}, pod_ref`. **Ownership is the `parent_id` tree** (≤1 parent, NULL at root, acyclic — carries decomposition + the authority/budget ceiling flowing down). **Relationships are EDGES, not node fields** (`depends_on, produces, verifies, answers, supports, supersedes, references`); a node owned once can be reused by many via edges. Plus an open **context tier** (NL `content`, `payload`) read by LLMs on demand — only the engine tier is schema'd, so it never ossifies into a brittle KG.
- **Derived, never stored:** `ready` / `blocked` / `stale` — computed from the graph + statuses.
- `satisfied_when ∈ {tool_success, all_owned_children_done, verified_by:<id>, user_signoff, quality_bar:{metric,≥t}}`.
- Assumption = `Evidence(status=assumed)`; Decision = a Plan node's rationale; Blocker = derived. (That's why those aren't node types.)

## Class lineage — parallel to the live Orchestrator → Manager → Agent trio
| live (Message-native) | new (WorkObject-native) |
|---|---|
| Orchestrator | **WorkOrchestrator** — a work-tree version of the live Orchestrator: REUSES its `run_loop`, thread pool, dependency-gated scheduling, monitoring/cancel, AND its three brains (architect/facts_curator/router). **Only its STATE becomes the WorkObject graph** (the blackboard job-map → nodes/edges/status). Parallel managers; sequential within each. |
| Manager | **WorkManager** — discharges one node/subtree: `project → run WorkAgent → apply mutations (writer) → run tool nodes → report status`. |
| Agent / Planner | **WorkAgent** — a normal agent whose task is a projected node and whose `allowed_tools` include the WorkGraph read+write family (`work_objects/tools.py`). It runs its normal loop on the blackboard and CALLS those tools to manipulate the graph; the tool family *is* the mutation vocabulary. A WorkAgent works the task node it is dispatched **and creates that node's sub-nodes** (its checklist → subtask nodes, via the reconcile hook) — this is correct and built; the manager is NOT changing. Decomposition happens at **two levels**: the architect splits the GOAL into top-level task nodes (one per manager), and each manager's planner splits ITS task node into sub-nodes. |

The 65 live Message-native agents and managers are untouched.

## The four interfaces (the real spec)
1. **Orchestrator → Manager:** `work_ref + node_id` + scope envelope — a subgraph handoff *by reference* (replaces today's lossy `dep_bundle` flatten into `information`).
2. **Manager → Agent:** a *role projection* — the node + its dependency Evidence + the contract + goal/constraints.
3. **Agent ↔ Graph (TOOLS, not one structured blob):** the agent manipulates the graph through a tool family — READ `graph_summary`/`graph_peek`/`graph_neighbors`/`graph_search` (progressive disclosure: see little by default, drill down on demand) + WRITE `add_subtask`/`add_dependency`/`record_finding`/`produce_artifact`/`ask_question`/`defer`/`mark_satisfied|failed|abandon` (thin wrappers over the writer in `work_objects/tools.py`). It runs its normal tool loop on the blackboard; only major evidence/artifacts/status land as nodes — harvested at the end, or via `record_finding` *sooner* so the curator sees them.
4. **Manager → Orchestrator:** status `{satisfied|waiting|failed|expanded}` + applied mutations.

## Brain agents under WorkObjects — REUSE the live Orchestrator's three brains VERBATIM
> Corrected 2026-06-18. The earlier "decentralized / architect-dissolved / curator-as-whole-graph-monitor" design below was ABANDONED. The substrate is a **work-tree version of the live Orchestrator**: same loop, same thread pool, same three brains, same up-front centralized architect — **only the orchestrator's STATE becomes the WorkObject graph.** Do not reintroduce the decentralized model.

The live Orchestrator runs three brains: **architect** (`shared::orchestrator_architect`, one-shot — decomposes the request into a spawn DAG), **facts_curator** (`shared::orchestrator_facts_curator`, delta-gated — decides `is_done`/`missing_requirements` + a `facts_patch`), **router** (`shared::orchestrator_router` — broadcast/cancellation + which manager handles a child). The WorkObject substrate **reuses all three unchanged**:

- **Architect — one-shot, UP-FRONT, centralized.** Emits `spawn: List[ArchitectJob{job_id, manager_type, sub_task_for_manager, depends_on}]`; each job is written 1:1 as a WorkObject node (`job_id`→node id, `manager_type`→`owner_agent` for routing, `sub_task_for_manager`→content, `depends_on`→edges). It is NOT dissolved into the planners; the DAG is built up front, and re-runs to EXPAND on `missing_requirements`.
- **Router.** Reused as-is: routes the ready-set + emits broadcast/cancellation (cancellation → `set_status abandoned` on the targeted nodes; broadcast is a no-op under sequential-within-a-manager dispatch).
- **Curator (facts_curator) — delta-gated semantic judge.** Runs only on a delta (a node reaching terminal status = the progress unit; batched per the live ≥10s/≥20s gate) and decides `is_done`/`missing_requirements`. The graph is the durable memory (Evidence/Artifact/pod nodes); the blackboard holds ephemeral scratch. `done = curator.is_done OR the satisfied_when rollup` — the curator's `is_done` is the semantic ceiling, the rollup is the floor/backstop.

**Parallel managers solve the graph:** the orchestrator fans the ready-set out to managers running CONCURRENTLY (the live thread pool / dependency-gated scheduling), and **each manager runs its own inner loop SEQUENTIALLY**. Two levels — parallel *managers*, sequential *within* a manager.

(The richer whole-graph curation — staleness/contradiction/duplication/promotion — is a LATER curator and is DEFERRED. v1 reuses the live `facts_curator` as-is.)

## Hard-won principles
- **Validated writer, not free LLM edits.** Mutations go through allowed-transition + authority-ceiling guards (the `dayflow_item_writer` discipline).
- **Event log = truth; node table = rebuildable projection** (the belief-engine pattern).
- **Subgraph handoffs only at delegation boundaries** (orch↔manager↔sub-orch). Intra-manager agent chatter stays blackboard (ephemeral, fast). Same gate as node-minting.
- **A node is a contract; decomposition is the owner's private implementation.** Authority/budget flow DOWN as a ceiling.
- **Deferred nodes MUST carry a `wake_on`/revisit condition** or they rot (the dayflow reloop / "synopses age out of view" failure).
- **Evidence/Artifact reference pods; durable conclusions promote to beliefs/KG.** Don't build a third knowledge store.
- **Two write tiers: blackboard for ephemeral, graph for durable.** Agents keep the local blackboard for working state + tool results (fast, lossy, dies with the turn); they record only **major evidence / artifacts / status** as graph nodes — the durable, curator-monitored truth. Same gate as node-minting and subgraph-handoffs.
- **Scale down:** a trivial WorkObject is `Goal + Tool + Artifact`; nearly every contract field is optional.
- Centralized, up-front architect: the architect builds the spawn DAG **before** dispatch (mirroring the live Orchestrator) — "the plan" is that DAG, NOT something emergent from decentralized per-node planners. Global properties (budget/deadline/no-dup) are enforced by the contract + goal constraints + the delta-gated curator; the architect re-runs to expand on `missing_requirements`.

## v1 scope
**Build:** Goal/Subtask/Tool/Evidence/Question/Artifact; ownership via the `parent_id` tree; DAG edges `depends_on`/`produces`/`answers`; `satisfied_when ∈ {tool_success, all_owned_children_done, user_signoff}`; the mutation events + validated writer; the ready-set scheduler; `wake_on{time,user_reply}`; pod refs; the executor projection; the three thin classes reusing the live leaf runtime.
**Defer:** Verification + `quality_bar`; automatic staleness/invalidation (lazy/manual only — that's the provenance daemon we shelved); `supports`/`contradicts`; Plan-alternatives; budget *enforcement*; the whole Mission tier.

**Proving ground:** the **sleep-cooling WorkObject** — Goal "stop overnight cooling at 6 AM" → a `wake_on:time` node → Tool: Nest setpoint → Artifact: confirmation → Evidence: it fired. Exercises dispatch + a wait + a tool + an evidence node without any of the fancy parts. (This is literally one experiment's intervention in a future Sleep MissionObject.)

## Open decisions
- Storage substrate: own SQLite tables (decided: **NOT** `unified_log_2026`/Messages).
- Subgraph rides *inside* the Message (`message.work_ref` + projected context — v1, minimal) vs a native `handle_work(work_ref, node_id)` entrypoint (v2, native).
- Decomposition trigger: eager-on-pickup vs lazy-on-failure.
- MissionObject = a `Goal.kind=standing` vs a distinct top-level object.

## Layout (intended)
```
work_objects/
  README.md            # this
  model.py             # WorkObject, WorkNode, Contract, edges, enums (pydantic)
  store.py             # event log + projection (own SQLite); the validated writer
  orchestrator.py      # WorkOrchestrator: resumable tick + ready-set + dispatch
  manager.py           # WorkManager: per-node execute loop
  agent.py             # WorkAgent: mutation-emitting planner
  scenarios/           # worked examples incl. the sleep-cooling WorkObject
```
