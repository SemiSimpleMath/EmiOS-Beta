# WorkObject Execution Architecture — Worker Versions of the Live Stack

**Status:** design of 2026-06-18; partially shipped. The **inner loop (§4–5b) is LIVE** as
`work_emi_team_manager` (render node + `WorkPlanner` reconcile hook). The **outer loop (§6–6b,
the `WorkOrchestrator`) was superseded** by the dayflow orchestrator pipeline — evaluator →
finalizer → architect → repair → state_mover promotion → materializer/switchboard dispatch —
see `docs/architecture/05_DAYFLOW.md`. The **WorkObject data model + taxonomy in `README.md`
is unchanged and still authoritative** — this doc only changes *how work is executed over it*.

---

## 1. Principle

Build the worker layer as a **~5% diff on the existing `Agent` / `Planner` / `MultiAgentManager`
/ `Orchestrator` classes** — not a parallel engine.

The bugs that ate the 2026-06-17/18 sessions (agents looping, repeating work, peek-looping,
not terminating, dependency results not reaching a synthesis) are **already solved** in the
live Planner + Manager + control-node cycle. We hit them only because we reinvented that cycle
in a from-scratch `WorkOrchestrator` + thin `WorkManager` + a hand-rolled projection. Reuse the
proven machinery; add only what is genuinely WorkObject-specific.

**The entire "WorkObject-ness" concentrates in ONE place: rendering a node (+ its tree) into
what the planner sees.** Get that render + the prompt + the work tools right and Planner/Manager
are ~95% reuse.

---

## 2. Keep / New / Drop

| | |
|---|---|
| **KEEP (genuinely good)** | `model.py` + `store.py` — the durable, event-sourced typed graph (validated writer, status transitions, rollup). `work_tools.py` / `tools.py` — the `work_*` graph-mutation vocabulary. |
| **NEW (thin)** | Worker versions of the 4 classes + one control node (`workobject_render_node`) + the worker agent definitions. |
| **DROP** | from-scratch `WorkOrchestrator` (the resumable tick), thin `WorkManager` + custom `_project`, the runtime-registered `work::planner` config in `work_agent.py`, `agent_runner.py`'s external blackboard-poking. |

---

## 3. Per-class diff

| Worker class | Base | Diff |
|---|---|---|
| **WorkAgent** | `Agent` | **~0.** Pure machinery (LLM call, prompt build, context injection, tool-result handling, recent_history). Untouched base. |
| **WorkPlanner** | `Planner` | **One real diff: a reconcile hook** in `process_llm_result` (sibling to `_mint_research_findings`) that mirrors the emitted `checklist` → subtask nodes and `progress` → Evidence nodes — declaratively, **no tool calls** (§5b). Otherwise inherited: the action/tool loop, recent_history, critic. The form **keeps `checklist`/`progress`** (repurposed; checklist items carry a stable id). |
| **WorkMultiAgentManager** | `MultiAgentManager` | **Small.** `request_handler` (live line ~263) stashes `work_id`/`node_id` (from message `data`) and sets the work contextvar before the loop. The **render node** does the real work. `_run_loop`, delegator, control nodes, scope/tool-scope, pipeline — all inherited. |
| **WorkOrchestrator** | `Orchestrator` | **The only real surgery** (§6). Architect writes the graph, curator reads it, state lives in the store (resumable) instead of `FactsState`, + park/resume. Likely **copy-and-modify**, not thin subclass. |

**Inherit vs copy (open):** lean *inherit* for Planner/Manager (diff ≈ 0, no upstream drift);
*copy-modify* for the Orchestrator (diff touches its state-handling broadly). Final call: coupling
vs. self-containment — TBD.

---

## 4. The inner loop — `workobject_render_node`

A real `ControlNode` at `app/assistant/control_nodes/workobject_render_node.py` (first-class →
scanned at boot; this is what was impossible while work_objects was runtime-isolated).

- Reads the work context (store + `node_id`) via the same `runtime.get_work_context()` contextvar
  the `work_*` tools use → render node and tools stay consistent.
- Loads the WorkObject and writes `work_projection` to the blackboard (the WorkPlanner's
  `user_context_items` renders it into the prompt).
- Routing is pure `state_map`; the node only preps state.

**state_map (re-render before every planner turn):**
```
delegator                    -> workobject_render_node
workobject_render_node       -> work::planner
work::planner                -> tool_caller
tool_result_handler          -> tool_return_router
tool_return_router           -> workobject_render_node     # ← fresh node view each cycle
work::planner_return_control -> manager_exit_node
```

The loop-back is the point: the planner re-sees its node **fresh each cycle** (subtasks it added,
statuses it changed) while `recent_history` (free from Planner) carries what it did. Current-state
+ history together → no frozen projection, no amnesia, no peek-loop. (The earlier re-projection bug
was re-projecting *without* history; here we have both.)

---

## 5. The render format (what the WorkPlanner sees)

Produced fresh each cycle by the render node. The *one real artifact* to get right.

```
## YOUR NODE  (the task you own)
TYPE/STATUS · TITLE · TASK · SUCCESS-WHEN · PARENT GOAL

## YOUR CHECKLIST  (this node's child subtasks — add/update with work_add_subtask / work_finish)
- [status] subtask title            (depends_on: …)
  …                                  ← this replaces the planner's free-text checklist

## DEPENDENCIES  (upstream nodes ALREADY solved — use their outputs directly; do NOT peek them)
- dep title:
    * [evidence] claim            -> content
    * [artifact] title            -> pod_ref / content

## THE WORK TREE  (summaries only; drill in with work_graph_peek)
- [type/status] node title
  …

## NEW / RELEVANT SINCE YOUR LAST TURN   (delivered by the router — §6)
- node X produced: finding Y   (relevant to you because …)
```

Plus `recent_history` (inherited) = this planner's own tool calls / results / critic.
Dependency **content** is carried in the projection (already fixed in `_project`/`render_projection`)
so a synthesis reads its inputs here instead of peek-looping.

---

## 5b. WorkerPlanner output → graph (the reconcile hook)

**Subtasks/evidence are managed through the planner's pydantic output, NOT tool calls.**
One tool per turn means per-item `work_add_subtask`/`work_finish` would burn dozens of
bookkeeping turns. Instead we reuse what the base `Planner` already does:
`_mint_research_findings` syncs the `findings_to_pod` form field → durable pods inside
`process_llm_result`, **zero tool calls**. The WorkPlanner adds **one sibling hook** doing the
same for `checklist` and `progress`.

**The checklist is a round-trip; we only swap its store (blackboard → graph).**
`emi_team::planner` already round-trips: it emits `checklist` (out), the blackboard holds it, and
`user.j2` renders *"the previous checklist you have to now update"* (in). The WorkObject routes
that round-trip through the **graph**:
- **out** — the reconcile hook writes emitted `checklist` → child subtask nodes (new → `add_node`;
  DONE → `set_status done` + attach evidence) and `progress` → Evidence nodes;
- **in** — `workobject_render_node` renders those nodes back into "## YOUR CHECKLIST" /
  "## NEW SINCE LAST TURN" (§5).

Same form, same "update the previous checklist" instruction, same `recent_history` — only the
store changes.

**The single `action` channel stays free for real work** (`search_web` / `web_manager` /
`work_produce_artifact` / `work_finish`). The live **DONE-next-turn discipline** (`system.j2`:
*"only mark DONE after the tool returned success in Recent History; never in the same turn as the
action"*) makes it clean — `action` does the work this turn, the checklist marks it done next turn,
declaratively. No turn spent on bookkeeping.

**Identity (the one mechanism detail).** Live planners match checklist items by *stable text*. To
reconcile to nodes we need stable ids: the render node tags each subtask (`[s1]…`), the planner
echoes the tag — the `Finding.unit` pattern (re-emit same id ⇒ upsert, not duplicate). So
`checklist` carries an id per item (`List[{id, text, status, evidence}]`, or an `[s1]` prefix).

**Durability — significance split** (answers "keep all sub-nodes or subsume into the pod?"). Like
normal research, the node's **conclusion is a research pod** (`node → produces → pod`) — what
dependents + the final synthesis consume. Plus:
- **`progress` → durable Evidence nodes** — the *significant* discoveries; they outlive the node
  (curator/router share them across node-agents; the synthesis uses them). Not subsumed.
- **checklist subtasks → execution-time scaffolding** — exist during the run (render + resume),
  done-marked as the planner goes; on completion **subsumed by the pod for consumption** (retained
  in the event log as provenance, never surfaced; a dependent's DEPENDENCIES block shows the node's
  *result*, never its checklist).

Consumers see normal-research's clean `node → pod (+ a few Evidence)`; the working agent + resume +
curator see the full tree. The keep-vs-subsume line is exactly the **curator's significance
judgment**.

**Reconcile placement (settled).** The hook lives in `WorkPlanner.process_llm_result` (runs every
turn, zero routing change). A **post-planner control node** (`workobject_reconcile_node`) is the
alternative — cleaner (vanilla `Planner`, no subclass) but must be wired onto each routing path;
it's also the home for post-processing that needs the tool *result* (which the planner hook can't
see). **Validated web-free 2026-06-18** (`worker_inner_loop_smoke`): a 4-item checklist reconciled
to 4 subtask nodes with no `work_add_subtask` calls, each closed (`proposed→active→done`) the turn
after its artifact, node rolled up to done. The render round-trip + reconcile + state_map all hold.

---

## 6. The WorkOrchestrator — live brains, graph substrate

The live `Orchestrator` already gives architect (DAG plan), parallel scheduling, curator (done),
router (inform/cancel/replan). Three repoints make it WorkObject-native; state becomes durable.

### Architect — creates / expands the graph
Called at the same two sites as today (`run_loop` init; replan when `replan_needed` or stalled),
but output flips from ephemeral spawn-specs to **durable graph mutations**:
- **init →** `create_work_object` + the first-level major branches (replaces `architect.seed`).
- **replan →** reads the **graph state** (which nodes are done, what evidence/artifacts exist,
  what's missing) and emits `add_node`/`add_edge` to **expand**, or "nothing to add."

**Two altitudes of graph growth (so they don't collide):**
- **Architect = global / strategic.** Sees the whole object + all results. Big concern-split at
  init; cross-branch re-planning no single node can see ("A and B done, goal still needs C → add C").
- **WorkPlanner = local / tactical.** Sees only its own node + deps. Decomposes *its* node into the
  checklist subtasks (§4–5).
- Boundary (proposed): architect creates goal + top branches; each branch's WorkPlanner refines its
  own subtree; architect re-enters only to expand globally on progress.

### Curator + Router — the cross-agent nervous system (original spirit, intact)
Their live spirit (read from the prompts) is **getting significant discoveries to the agents who
care**, *not* graph hygiene:
- **Curator** = distiller. "Update shared facts based on child results… small, stable,
  non-contradictory… only promote what's supported." Flags which new graph discoveries are
  *significant*; dedupe/contradiction; goal-done check. In the WorkObject world the graph *stores*
  the content, so curated "facts" become **lightweight highlights/pointers over nodes**, not a
  separate `FactsState` blob.
- **Router** = information bus. "Decide who should be informed (broadcast)… only running jobs…
  don't echo the source… don't repeat unless materially changed." Picks which *running*
  WorkPlanners care about a discovery. (Cancel/replan are side-duties.)

**The loop, with the render node as the delivery surface:**
> WorkPlanner A records evidence → **curator** marks it significant → **router** says "node B cares"
> → **render node** surfaces it to B as the "NEW / RELEVANT SINCE YOUR LAST TURN" block next cycle.

Graph = durable memory. Curator+router = relevance routing (live spirit). Render node = per-agent
delivery. Nothing collapses; each keeps its job, reading/writing the graph instead of `FactsState`.

### State, resumability, parallelism
- **Durable state:** the orchestrator's job-DAG / results / facts move into the **graph**
  (jobs↔nodes, results↔evidence/artifacts, `depends_on`↔edges). Resumable across process death.
- **park / resume on `wake_at`** — the one genuinely-new mechanic (a node waiting on time/event is
  parked; the loop advances to the earliest wake). Carry over from the old `WorkOrchestrator`.
- **Parallelism:** PARALLEL managers — the orchestrator fans the ready-set out to managers running
  CONCURRENTLY (the live thread pool / dependency-gated scheduling). The "workers don't spawn
  parallel workers" decision applies *within* a manager (each manager's inner loop is sequential),
  NOT at the orchestrator level. **Two levels: parallel managers, sequential within each.** (Resumable
  park/resume on `wake_at` is a LATER layer on top of the live blocking run_loop, not the initial mirror.)

---

## 6b. WorkOrchestrator build plan (the surgery)

`work_objects/WorkOrchestrator.py` is a verbatim copy of the live `Orchestrator`. **Keep** its
brain-agent invocation + cooperative-cancel machinery; **repoint** state + scheduling +
architect-output to the **graph**. The loop becomes a resumable **graph tick** (the dropped
`orchestrator.py` already proved this shape — reuse its tick/park-resume logic).

**Seams (live line refs):**
- **init architect (≈432):** instead of `_normalize_spawn_specs`, the architect writes the graph —
  `create_work_object` (goal) + first-level `add_node` branches. (Replaces `architect.seed`.)
- **`_schedule_children` (≈442, 699):** dispatch the graph's **ready nodes** (`wo.ready_nodes(now)`
  — deps satisfied, wake cleared) to **work managers** via `work_runtime.run_node` per node (thread
  pool, parallelism conservative), de-duped by node status. Replaces spawn-spec scheduling.
- **`facts_curator` (≈564):** reads the **graph** (events since last seq + new Evidence), not
  child-result dicts → emits `is_done` / **`moot`** / significance highlights. "Facts" become
  lightweight highlights over Evidence nodes; no `FactsState`.
- **`router` (≈584):** unchanged in spirit — `cancel_running` + highlight; on `moot`, cancel ALL.
- **moot→abort (≈601-622, ALREADY present):** feed the curator's graph-derived `moot` in; the
  existing `_request_cancel_running_children` + finalize do the rest. **This is the Val Kilmer path.**
- **state:** drop `FactsState` / `_pending_jobs` / in-memory results → the **graph** is the state
  (resumable); re-derive ready/running/done from the store each tick.
- **park/resume on `wake_at`** (from the old tick): park on the earliest future wake; advance `now`.

**Moot → abort flow (Val Kilmer — the curator/router demo):**
1. The "food" node-agent's `progress`: *"Val Kilmer died Apr 2025"* → reconcile → Evidence node.
2. Curator tick reads the new Evidence → judges the GOAL **moot** (premise invalid) + done_reason.
3. Orchestrator `_request_cancel_running_children` cancels the still-running games + conversation
   managers (cooperative cancel — the manager loop checks `cancelled`/`cancel`, ≈507).
4. WorkObject set `moot`/aborted (terminal, NOT done-success); final answer = the moot reason.

**Slices (validate each web-free):**
- **2a — multi-node dispatch (foundation) ✅ DONE (web-free, 2026-06-18):** orchestrator dispatched 3
  nodes through the inner loop; each produced an artifact + finished; goal rolled up. Needed a prompt
  fix (single-step node → no checklist; checklist items = work-outcomes, not your own actions) to stop
  over-decomposition. **2b requirement it surfaced:** the orchestrator must treat a dispatched node as
  a BLACK BOX — `run_node` owns its whole subtree (the planner's checklist), so the ready-set is
  *architect-created nodes not yet dispatched*, NOT every ready node (the old greedy `ready_nodes`,
  used here, would otherwise grab a planner's internal checklist subtasks).
- **2b — curator + moot/abort.** Orchestrator LOGIC ✅ done (`work_orchestrator.py`; deterministic
  test `orchestrator_2b_det` 8/8: dispatch-scoping + **black-box** (won't dispatch a dispatched
  node's planner-internal subtasks — fixes 2a) + **moot→abort** (curator says moot → remaining nodes
  never dispatched, goal + rest abandoned)). Sequential, so "cancel running managers" = stop
  dispatching (no thread-kill). **LLM curator ✅ done** (`worker_agents::curator` — plain Agent,
  modeled on the live facts_curator with `moot` made explicit; `make_curate` renders the graph +
  runs it as the `curate` hook). Web-free integration `orchestrator_2b_moot` green: a node recorded
  a (scenario-primed, never prompt-primed) "subject not returning" finding and the curator, on its
  GENERIC prompt + the graph view alone, judged the objective moot → remaining node aborted, goal
  abandoned. REMAINING: the real **Val Kilmer** web run (organic discovery via web_manager — no
  priming anywhere).
- **2c — router highlights (cross-agent "new since last turn") + park/resume.**

---

## 7. Where things live (per the directive)

- Worker **agent definitions** (config + prompts + form): `app/assistant/agents/worker_agents/…`
  (first-class, auto-discovered by `agents_dir.rglob`). e.g. `worker_agents/planner/`.
- Worker **classes** (`class_name` → `agent_classes/<Name>.py`): `WorkPlanner`, render node under
  `control_nodes/`, manager/orchestrator under their class dirs — **if** we go the inherit/first-class
  route. (vs. self-contained copies in `work_objects/` — the open inherit-vs-copy call, §3.)
- This is a **shift from the old isolation discipline**: the worker layer becomes part of the main
  repo (pushable), unlike the excluded `work_objects/`. Deliberate.

---

## 8. Decisions — settled vs. open

**Settled this session:**
- **No sub-agent spawning for subtasks.** A node is worked by ONE agent doing its own node + its own
  checklist subtasks IN SEQUENCE. Many agents exist per WorkObject, but the **architect** creates
  nodes and assigns them to managers — node→agent dispatch lives there, not in an agent farming out
  its checklist.
- **Subtasks/`progress` via the pydantic reconcile hook, not tool calls** (§5b).
- **Checklist-item identity = stable ids** (render assigns, planner echoes; `Finding.unit` pattern).
- **Durability = significance split** (conclusion→pod, significant→Evidence, checklist→scaffolding).
- **Curator/router operate WITHIN a WorkObject** (across its many node-agents), not only across WorkObjects.

**Open:**
1. **Inherit vs copy** per class (§3).
2. **Architect ↔ WorkPlanner growth boundary** (§6) — how much the architect decomposes vs. leaves
   to node-level WorkPlanners.
3. **Render format** details (§5) — tree depth, highlight wording, how much of the whole tree vs.
   just the relevant subtree.
4. Where the **WorkStore** comes from in first-class mode (DI service vs. contextvar-carried).

---

## 9. Build order
1. **Inner loop — ✅ DONE, validated web-free (2026-06-18).** Built: `agent_classes/WorkPlanner.py`
   (Planner + reconcile hook), `control_nodes/workobject_render_node.py`, `agents/worker_agents/planner/`
   (config + prompts + id'd-checklist form), `work_objects/work_runtime.py` (driver + manager state_map §4).
   `worker_inner_loop_smoke` green: checklist→subtask nodes (no tool calls), per-turn status sync, rollup to done.
2. **WorkOrchestrator:** architect create/expand (§6) + durable state + park/resume.
3. **Curator → router → render highlight path** (§6) — the cross-agent nervous system.
