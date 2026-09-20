# Dayflow Orchestrator

The Dayflow Orchestrator is the autonomous daily workflow engine — a background "day planner AI" that
continuously turns intake (email, chat, calendar-driven routine, delegations) into executed work,
reminders, and questions for the user.

## Core Philosophy

**Everything actionable is a WORK OBJECT** — a goal plus a small DAG of typed nodes in a durable,
event-sourced graph store. Even a one-shot action is a one-node work object. Items (the older Message
substrate) are intake and context only; the evaluator is the sole path from intake to action.
The substrate itself — model, store, invariants, wake primitives, runtime, and every graph writer —
has its own reference: [08_WORK_OBJECTS.md](08_WORK_OBJECTS.md).

Every graph mutation goes through a validated writer (allowed transitions per node family, authority
ceilings, atomic event + projection). Nothing silently disappears: results are recorded as evidence,
failures surface loudly for adjudication, and user feedback is authoritative at every layer.

Sharp role separation, one bounded LLM judgment per role, deterministic mechanics everywhere else:

| Role | Decides | Deterministic guard |
|------|---------|---------------------|
| evaluator (`strategic_planner_wo`) | WHAT work exists (create/change/re-plan/complete/abandon) | `work_persist` applies; intake consumed with provenance |
| work_architect | the STRUCTURE of one goal (DAG + wake gates + dedupe) | `work_architect_apply` projects the delta; a dedupe prune is licensed only after the named survivor is verified live |
| state_mover | did the awaited event arrive; is now the wrong moment | promotion itself is deterministic `is_ready` (time + deps) |
| switchboard | WHERE one ready node goes, by READING its goal | two routes only: ticket the user, or run the worker |
| work_finalizer | was ONE node's GOAL achieved (achieved / achieved_plan_changes / retry / unrecoverable) + an `outcome` account for the planner | sole producer of `closed`; `is_satisfied` keys on `closed`; `_STATUS_FOR` maps each verdict to a status sequence; repeated failure is escalated to `ask_user` deterministically |
| worker (`work_emi_team_manager`) | HOW a node gets done | render-loop manager; results land as evidence children |

`work_repair` was retired on 2026-09-16; its three dispositions moved to the finalizer's verdicts
(retry -> `retry`, which must NAME what will differ; escalate -> `unrecoverable` + `ask_user`, and the
architect plans the ask; abandon -> `unrecoverable` + `stop` for a branch, the steward for a whole goal).
Its files remain on disk, unwired. The repeated-failure bound is `work_finalizer_node._REPEAT_FAILURE_LIMIT`
against the goal's `goal_unmet_attempts` (2026-09-17).

## Architecture

```
DayflowScheduler (event-driven, debounced; precise per-node time wakes; work-progress follow-ups)
  -> dayflow_orchestrator_cadence_tick()
    -> run_dayflow_ingestion()          (chat / email / delegation / pods -> items table)
    -> four sweeps                      (legacy stale / orphaned / zombie items, then stuck work nodes)
    -> Invoke dayflow_orchestrator_manager (state_map order):
         intake_triage -> triage_persist -> context_enricher
           -> strategic_planner_wo (EVALUATOR) -> strategic_planner_wo_persist
           -> work_architect_node
           -> state_mover -> state_mover_persist (node promotion + event wakes)
           -> work_node_materializer -> action_selector -> switchboard
           -> work_node_dispatch   (CLAIMS the node, opens the dispatch room, ENDS THE PASS)
           -> post_room_finalize -> final_answer

  per fired time-wake (DayflowScheduler._fire_work_node), serialized with the tick by _run_gate:
    -> Invoke dayflow_wake_manager   (THE WAKE PASS — no planning stage exists in it):
         work_node_wake_prep (stage the ONE due node) -> state_mover -> state_mover_persist
           -> work_node_wake_router (actionable -> dispatch; held/ended -> finalize)
           -> switchboard -> work_node_dispatch -> post_room_finalize -> final_answer

  and, per claimed node, on its own thread (work_session.open_session):
    -> Invoke dayflow_dispatch_manager:
         dayflow_switchboard_arguments_node -> dayflow_tool_caller -> work_finalizer_node -> exit
```

The pipeline is fixed by the manager's `state_map`
(`multi_agents/dayflow_orchestrator_manager/config.yaml`), not by free agent handoffs.

**The planning pass ends at the claim.** Once a node is `dispatched`, the graph is stable — every other
consumer reads it as in-flight and plans around it — so that state, not an elapsed timer, is what makes
it safe for the next pass to start. The call itself (build arguments, invoke the tool, judge the result)
runs in `dayflow_dispatch_manager`: one room per claimed node, its own manager instance, its own
blackboard, its own thread.

This matters because a tool can block for a long time. `create_dayflow_ticket` holds its call open for
the whole ask window so the user's reply comes back as the tool's RESULT. With the call inside the tick,
and `DayflowScheduler` admitting one cadence tick at a time and spacing the next from the previous
tick's FINISH, a single unanswered notify was an hour in which nothing planned, woke, or dispatched.

Concurrency is safe in the dispatch room and not in planning, because the inputs differ. Two planning
passes read the same portfolio and the same intake, and their only defence against both converting the
same email is a prompt telling the model to check. A dispatch room's input is ONE node held
exclusively: the claim is an exclusive `claim_task` operation under the store transaction, so a second
room attempting the same node is refused. Exclusion lives on the work item, not on a global flag.

Planning passes are mutually exclusive, and since 2026-09-18 so are wake passes: both hold the
scheduler's `_run_gate` for the length of a manager invocation. A wake is its own manager
(`dayflow_wake_manager`) precisely so that it cannot plan: it has no intake, steward or architect to
fall into. (It used to be a routing hint inside the orchestrator that the router never saw, so every
wake ran the full pipeline.)

## The work object lifecycle

**Creation.** The evaluator judges the portfolio + new intake each tick and emits only WHAT changed:
new/changed objectives (with a prose `rationale` brief for the architect and `based_on` provenance),
`replan_work_ids`, `complete_work_ids`, `abandon_work_ids`. Consumed intake items are closed
(`converted_to_work_object:<id>`) and their summaries folded into the goal content. Its context is
id-chain annotated (2026-08-01): ticket replies render with their resolved work object and status
(`[work_x — done]` via `trigger_context.work_node`), and TODAY'S SCHEDULE renders as
`expected_schedule_view` with provenance chased ticket → node → work object ("outcome of work_x —
done; user willdo") — the tracker copies verbatim ticket ids into schedule-item `source`, and the
prep node does the joins deterministically; the model judges, it never does record linkage.
Subconscious concerns render with `[concern:<prefix>]` ids for citation in `based_on`.

**Worker provenance.** Descendants created inside an assigned task are execution-history
records, not independently schedulable nodes. Only direct subtask children of the goal
are orchestrator assignments. A takeover worker and the finalizer read the full owned
history; the architect and steward receive finalizer summaries. Internal failures do
not independently increment the goal's failure count. See 08_WORK_OBJECTS.md.

**Decomposition.** The architect turns one goal into 1-5 subtask nodes under the goal node, with
`depends_on` edges and at most one wake primitive per node:
- `wake_at` — a deterministic time (absolute ISO datetime; elapsed time is always this),
- `wake_ref` — a prose external-event condition the state_mover matches against incoming intake.
A node whose goal is to reach the user is written plainly ("Tell/Ask the user X") — the switchboard
decides delivery, the architect never picks channels. On re-plan the architect emits a DELTA: new nodes
plus `abandon_node_ids` for moot branches (pruned recursively, finished nodes kept as a record).

**Readiness and promotion.** `is_ready` (substrate, deterministic) = status in
proposed/waiting/actionable + `wake_at` passed + all `depends_on` satisfied. The state_mover persist node promotes every ready main task (direct subtask child of the goal) to
`actionable`; the state_mover LLM may HOLD a few (`held_work_nodes` with `reactivate_at`) for quiet
hours / meetings / user-away — the worst LLM failure is "acted when it could have waited", never a stuck
node. External-event (`wake_ref`) nodes are never promoted; the state_mover wakes them via `node_wakes`
when the awaited event appears in intake.

**Dispatch.** The materializer lists each actionable node as `work_id::node_id`; the action_selector
picks ONE; the switchboard reads the node's goal and routes it:
- **communicate with the user** (notify/remind/tell/ask — a UI ping) -> `create_dayflow_ticket`
- **everything else** (research, device/calendar/todo changes, composing and SENDING email/text to a
  recipient) -> `work_emi_team_manager` — the worker picks its own sub-managers/tools.

Both are ordinary tool names, and the dispatch that follows is identical for either: the gate claims the
node, `open_session` opens a dispatch room on its own thread, and inside that room
`dayflow_switchboard_arguments_node` builds the arguments FROM THE NODE (branch-free — every call
carries `work_id`, `node_id`, `task`, `information`), `dayflow_tool_caller` executes it, and
`work_finalizer_node` judges what comes back. Adding a third tool needs no dispatch code.

The session registers before its thread starts and stamps `payload.session_id` on the node.
Ask recovery consults the registry to avoid starting a second waiter; the sweeper does not read it.
Each tick supervises the in-flight set (`sweep_stuck_work_nodes`) using the newest write anywhere in
a node's subtree. More than 80 minutes of inactivity (the one-hour ask window plus 20 minutes)
fails the node. The result recorder rejects ended nodes and stale dispatch epochs.

`node_dispatch.dispatch_node` still exists but is no longer on any path; the state_map plus
`open_session` is the dispatch core.

**Asks (user_reply).** An ask is a TOOL CALL whose result is the user's reply. Surfacing it creates the
ticket (validity window = the call's timeout, currently 1h; a new ask ticket expires prior open asks of
the same work object) and marks the node `dispatched + wake_kind=user_reply` — in flight, exactly like a
worker job; one live ask per work object (a second ask node queues behind it). The reply/dismissal is
matched back by `trigger_context.work_node` plus `dispatch_epoch`, recorded as an EVIDENCE child (the node's `content` is its
immutable directive), and the node completes -> the finalizer judges the reply like any result. An
unanswered ticket expiring returns a ToolResult saying "user not reached". The recorder saves that
result as evidence and marks the node `done`; the dispatch finalizer judges whether the goal was
achieved. The sweeper records a failed timeout result with an epoch and inactivity fence. Cadence resumes the ordinary finalizer for that saved result.

The repeated-failure bound is on the GOAL: `goal_unmet_attempts` counts not-achieved finalizer judgments once per main-task dispatch attempt, and at
`_REPEAT_FAILURE_LIMIT` the finalizer node escalates the verdict to `ask_user` instead of re-opening the
step. It survives the architect re-planning under a new node id; it does not survive the steward minting
a fresh work object for the same action tomorrow.
Ticket text is composed by `ticket_builder_manager`
(multi_agents/): dispatch hands over ids and the goal (`work_id::node_id` + the node's directive),
a read-only planner pulls the SUBSTANCE the goal promises — `read_work_object` on the graph,
`pod_fetch` to dereference pod ids (never pod_search, never anything world-facing) — and the
composer writes the final message. The message IS the delivery: a deliver-goal's ticket carries
the produced result itself, and if the graph provably lacks it, the ticket says so honestly
(loud failure reveals broken work). Self-contained briefs return_control at action 0, so the
common case costs one planner step. The store fence refuses terminal writes on an in-flight ask
(replan cannot prune a question that is out).
A pre-surface ask (`proposed + user_reply`, no wake_at) promotes for its first surface via the
state_mover, which may HOLD it (a held pre-surface ask parks `waiting` and keeps `wake_kind=user_reply`
so a late reply to an earlier ticket still matches).

**Completion.** A worker-`done` node is only a RESULT. The finalizer runs in the DISPATCH ROOM, in the
same pass that made the call, and reads that node's full result. It answers ONE question — was the
node's goal achieved — and the tool's own status (returned / reported failure) is input to that
judgment, never a limit on it. Every verdict carries `outcome`: prose written for the planner, the
account of what was found and what was not. Then:

| verdict | meaning | writes |
|---|---|---|
| `achieved` | goal met (failed sub-steps are provenance in `outcome`) | `-> closed` (only `closed` counts toward the goal) |
| `achieved_plan_changes` | goal met, and the result changes the plan | `-> closed` + `recommendation` (route `plan_changes`); rollup holds the goal until the architect consumes it |
| `retry` | not met; minor; `recommendation` NAMES what will differ | `-> failed -> proposed` (counted once, back in the architect's inbox) |
| `unrecoverable` | not met, no recovery on this path; `next_step` = stop / new_approach / ask_user (+ `question_for_user`) | `-> failed` + the route |

`retry` must name the difference: re-running a step whose circumstances have not changed reproduces
its error. A single node's result never ends a whole work object; a finalizer that thinks the goal is
finished or moot says so in `outcome` and the steward rules next tick.

**Verdicts persist on the NODE**, not in memory: `nodes.payload.finalizer`, beside the terminal
epitaph. The architect that acts on a route runs on a LATER tick, and every tick builds
a fresh manager with a fresh blackboard, so anything left in memory is discarded before its reader
exists. `work_architect_node._pending_finalizer_instructions` reads them off the graph and
the atomic architect batch stamps the exact instruction it consumed, so a judgment made ticks ago stops arriving as fresh
advice. When all of the goal's children are closed, the store's rollup completes the work object.

**Closure is a transition with obligations (2026-07-31).** Entering `done`/`abandoned` — via the
steward or automatic rollup — cascade-abandons every still-startable node
(proposed/actionable/waiting/failed) and clears its wakes, so a closed object can never fire again;
`WorkObject.validate()` enforces the invariant (a terminal object holding a startable node is a
write-time error), and `repair_terminal_zombies()` healed pre-cascade rows at boot. Motivation: a
`done` object's leftover `waiting` node kept an armed timer and ghost-ticketed the user a day later.
If the closed work object carries `concern:` provenance (`constraints.concern_refs`, forwarded from
the evaluator's `based_on`), the outcome — with the user's recorded words — back-propagates to the
subconscious concerns register and triggers a cooldown-guarded noticer rerun
(`subconscious/concern_feedback.py`, 2026-08-01).

**Failure.** A node judged not-achieved carries the finalizer's verdict: `retry` re-opens it to the architect's
inbox with what will be different; `unrecoverable` leaves it failed with a typed route the architect
acts on next tick (prune the branch, plan a different approach, or plan the ONE node that asks the
user the given question). The steward sees the same outcome/recommendation on the node in the
portfolio (`FINALIZER (...)` / `RECOMMENDS` / `ASK THE USER` lines) and decides what becomes of the
goal. A failed node still blocks its goal (`is_satisfied` requires `closed`).

Dispatch-manager aborts/exceptions and inactivity timeouts use the common result recorder. Cadence resumes unjudged results without rerunning their tools. A swept timeout is eligible even if the old worker thread remains alive; stale results cannot overwrite it.

## DayflowScheduler

`dayflow_scheduler.py` — event-driven with two-tier throttling:
- `DEBOUNCE_SECONDS=60`, `MIN_GAP_SECONDS=120` (mutual-exclusion floor), `POKE_MIN_INTERVAL_SECONDS=600`
  (delta pokes: chat/email/AFK/ticket), `MAX_CEILING_SECONDS=1800`, `STARTUP_TICK_DELAY_SECONDS=45`.
- **Precise work-node wakes**: one APScheduler one-shot per time-gated node (`dayflow_work_wake::` jobs,
  re-armed after ticks and wake passes, restart-safe from the durable store; obsolete jobs are removed). Firing opens
  **`dayflow_wake_manager`** (`_fire_work_node`) — the WAKE PASS: `work_node_wake_prep_node` stages the
  one due node as the state_mover's only candidate, the state_mover re-judges the moment, the persist
  node promotes or parks THAT node only (`triggered_work_node` scopes it), and the wake router dispatches
  it through the same switchboard → `work_node_dispatch_node` tail the tick uses. No intake, steward or
  architect exists in that manager. **One pass at a time, ticks and wakes alike**: both lanes hold
  `_run_gate` for the length of the manager invocation, and a wake re-checks `is_ready` inside the gate
  so it sees what the pass before it wrote. (Until 2026-09-18 a wake was a routing hint inside the
  orchestrator — one the router never saw, so every wake ran the full planning pipeline ungated: three
  nodes sharing one `wake_at` were three architects on one graph.)
- **Ask recovery at boot**: `start()` calls `work_session.re_arm_inflight_asks()` before the first tick,
  so a question in flight when the process died is settled from its ticket rather than left orphaned.
- **Work-progress follow-up**: when a node reaches a result, a reply is recorded, or a dispatch leaves
  more ready nodes waiting, `dayflow_work_progress` schedules a prompt NON-poke tick (~MIN_GAP), so
  sequential chains and ready queues advance in minutes instead of one step per ceiling tick.
- **Item timers are retired** (2026-09-16): items are intake + context only; every timer is a work-node
  wake. The `fast_tick` branch of the router went with them.
- **Failure escalation**: 3 consecutive tick failures surface one owner-visible ticket via the ticket
  manager's direct write (which does not run this pipeline).

## Persistent state — two substrates

**Work objects** (`work_objects/` substrate): five tables in emi.db (`work_objects`, `nodes`, `edges`,
`events`, `actions`), opened via `dayflow_orchestrator/work_store.py`. The append-only event log is the source of
truth; nodes/edges are the rebuildable projection; every mutation is one short atomic transaction through
`WorkStore.apply` (allowed transitions per node family, authority ceilings, structural validation,
derived rollup). `DAYFLOW_WORK_DB` overrides the path for tests.

**Items** (`unified_log_2026`, `source='dayflow_item'`): intake + context. Upsert key `Message.id` =
`metadata.item_id`; numeric `short_id` for prompts; `state_store.py` reads, `dayflow_item_writer.py`
writes (`ALLOWED_TRANSITIONS` enforced). Freshness windows age untouched items out of the agents' view
(active >24h, closed >2h). Ingestion sources: cross-room chat (context, `closed`), important email
(`artifact`), master-room delegations (`user_request`), allowlisted pods. Calendar events are not
ingested — the evaluator sees them via `resource_expected_calendar` and the routine overlay.

**The item dispatch lane is gone (deleted 2026-09-16).** Items are intake and context only; the
evaluator is the sole intake -> action path. The dormancy window closed with zero traffic — no item had
armed a wake in the 30 days before removal — so the lane was removed rather than guarded: the
scheduler's item-timer scan (now `_arm_ceiling_tick`, heartbeat only), the `fast_tick` /
`triggered_item_id` plumbing, `fast_tick_promoter_node`, `view_materializer_node`,
`dispatch_sweeper.list_active_dispatches`, and the dispatcher's legacy-item close. A ref that is not
`work_id::node_id` now logs ERROR and dispatches nothing.

Two things went with it. `state_transition_guard_node` existed to validate and write the state_mover's
item `state_mutations`; the state_mover no longer emits them, so the node and the
`state_mutations_persisted_tf` handshake are gone and the state_map runs `state_mover ->
state_mover_persist` directly. And the state_mover's prompt lost the two-thirds of its text that taught
the item vocabulary (`important_open`, `watching`, `suppressed`, and item-meanings of `actionable` /
`waiting` / `dispatched` / `closed` that conflicted with the node meanings). The relevance_cleaner was
retired at the cutover; its files remain, unwired.

**What the state_mover is now.** Two judgment calls, both things the graph cannot do:
(1) *did the awaited event arrive?* — for nodes parked on `wake_kind in {event, signal}`, match the
recent intake and emit `node_wakes` with the arrived content as the worker's resume context;
(2) *is now the wrong moment?* — hold a ready node via `held_work_nodes` for quiet hours, a meeting, or
the user being away. Time and dependency gates are deterministic (`WorkObject.is_ready`), and every
ready node the LLM does not hold is promoted to `actionable`, so the worst failure is "promoted when it
could have waited", never a stuck node.

## Tickets

`create_dayflow_ticket` is a tool; ticket phrasing goes through `ticket_builder_manager`
(`CreateDayflowTicketTool._format_brief`) so the user sees a warm, first-person message rather than raw
node text. Ask tickets carry `trigger_context.work_node` for reply matching. Ticket state lives in the
ticket manager, not in dayflow items.

## Master Room Integration

- User chat in `master_room` blocks dayflow for 180s (`MASTER_ROOM_BLOCK_SECONDS`) so it doesn't talk over them.
- The master-room chat gate can delegate to dayflow (`dayflow_request` tagged messages, ingested next tick).
- Ticket responses poke the scheduler (`dayflow_ticket_responded`).

## Key Files

| File | Purpose |
|------|---------|
| `dayflow_orchestrator/dayflow_scheduler.py` | Event-driven scheduling, precise node wakes, follow-up ticks |
| `dayflow_orchestrator/dayflow_tick.py` | Cadence tick entry point |
| `dayflow_orchestrator/ingestion.py` + `input_message_builder.py` | Per-source intake -> items |
| `dayflow_orchestrator/work_store.py` | The dayflow WorkObject store (emi.db) |
| `dayflow_orchestrator/work_persist.py` | Applies the evaluator's output (mint/change/complete/abandon) |
| `dayflow_orchestrator/work_architect_apply.py` | Projects an architect DAG/delta onto the graph (+ licensed dedupe prunes) |
| `dayflow_orchestrator/work_session.py` | Opens a dispatch room per claimed node; session registry; ask recovery at boot |
| `dayflow_orchestrator/work_portfolio.py` | Strategic projection (failures loud, outcomes as node -> result) |
| `dayflow_orchestrator/node_dispatch.py` | `signal_work_progress` only — `dispatch_node` is vestigial, on no path |
| `dayflow_orchestrator/state_store.py` / `dayflow_item_writer.py` | Item substrate read / validated write |
| `dayflow_orchestrator/dispatch_sweeper.py` | Tick sweeps (stale / orphaned / zombie) |
| `control_nodes/strategic_planner_wo_prep/persist_node.py` | Evaluator context build / output apply |
| `control_nodes/work_finalizer_node.py` | Judges one call's result AND writes the verdict (runs in the dispatch room) |
| `control_nodes/work_architect_node.py` | Decompose new goals, re-plan flagged ones, read pending finalizer instructions |
| `control_nodes/dayflow_switchboard_arguments_node.py` / `dayflow_tool_caller.py` | Build the tool's arguments from the node; execute it |
| `control_nodes/state_mover_prep/persist_node.py` | Waits + promotion candidates / promotion + node wakes |
| `control_nodes/work_node_materializer_node.py` | Ready-node listing |
| `control_nodes/work_node_dispatch_node.py` | Carries out the switchboard's routing |
| `work_objects/model.py` / `store.py` | Substrate: graph model, validated writer, transitions |
| `work_objects/runtime.py` / `result_recorder.py` | Work context for a running node; the one writer of a tool result |
| `multi_agents/dayflow_dispatch_manager/config.yaml` | The dispatch room: one per claimed node, on its own thread |
| `multi_agents/work_emi_team_manager/config.yaml` | The worker: render-loop manager (DESIGN.md §4) |
| `agents/dayflow_orchestrator/` | The pipeline agents (evaluator, architect, switchboard, finalizer, state_mover, ...) |

## Dispatch and results (reworked 2026-09-16)

The principle: **there is no architectural difference between "check the user's email" and "send the user a
notify".** Both are tool calls made by the same dispatcher, returning results recorded the same way.
Every divergence found on 2026-09-16 traced to one root — when work objects arrived they were built
*beside* the existing machinery rather than *through* it, so each generic seam grew a work-object
special case and the work lane grew a private copy of each generic mechanism. **When the substrate
needs something the generic path has, route through it or change it — never copy it.**

**The gate claims.** `work_node_dispatch_node` marks the picked node `dispatched` (bumping
`dispatch_epoch`) BEFORE any tool is called, so from that instant every consumer — the ready set, the
portfolio, the next planning pass — sees it as taken. `open_session` and `discharge_node` now REFUSE
an unclaimed node rather than claiming it a second way. Previously the claim happened in three places
with different timing, and the ticket lane surfaced the question to the user *before* marking the
node, leaving a window where the user could answer a node that did not yet say it was asking.

**Everything dispatched is a tool call.** A manager is a tool (`ManagerInterface.invoke_on` — the
shared call: sub-manager scope seam, standard `task_request` message, structured tool errors). A
ticket is a tool (`create_dayflow_ticket`, which surfaces the question and blocks until the user
answers, closes it, or its validity window lapses, returning a ToolResult either way). Both run on a
session thread; the dispatching tick returns immediately.

**One recorder.** `work_objects/result_recorder.record_tool_result` is the only thing that turns a
result into graph state, for every lane: result as evidence, research pod attached, node out of
flight, epoch-fenced. It does not judge — `done` means "a result exists, the finalizer has not
ruled". The only distinction it makes is the one the ToolResult itself declares: a tool reporting an
error leaves the node `failed`, which the finalizer then judges. Outcome nuance ("expired, user not
reached") belongs in the result TEXT, which the finalizer reads in full.

**The worker cannot pre-empt its dispatcher.** `work_finish` records the worker's verdict as evidence
and returns control; it no longer writes the node's status. It used to, which meant the node was
already terminal when the manager returned and the recorder wrote nothing at all — not the status,
not the epoch fence, not the result.

**Supervision and long calls.** A session blocked inside one tool writes nothing meanwhile, so
the inactivity threshold must exceed a legitimate wait. `_WORK_NODE_FROZEN_TIMEOUT_S` is DERIVED from
the longest legitimate tool call (the ask window) plus a grace, rather than guessed — it cannot drift
if either number moves. Both a lost thread and a wedged call are detected by the same subtree
inactivity rule; there is no immediate session-liveness check. The threshold is 80 minutes today,
and failure is applied by the next planning tick that observes it exceeded.

**The finalizer judges and writes, in one node.** It was briefly two — one emitting a verdict onto the
blackboard, one reading it back and writing the graph. The agent call happens in that node, so its
schema was already in hand and the blackboard hop to a second state_map step carried nothing;
`work_architect_node` has always called its agent and written its graph in one place. Merged
2026-09-16. The separation that matters is still there: an agent decides meaning, deterministic code
turns the decision into graph state via `_STATUS_FOR`, and the store refuses any verdict that maps to
an illegal transition.

**Its reach is the node it judged.** A third verdict, RESOLVE, used to let it set the whole
WorkObject done or abandoned from a single node's result. That belongs to the STEWARD, which already
owns `complete_work_ids` / `abandon_work_ids` and sees every work object's outcomes each tick;
ordinary completion needs nobody, since the store's rollup completes a goal once `is_satisfied`. A
finalizer that thinks the goal is finished or moot says so in `outcome`, and the steward rules on
it next tick.

**The rollup yields to a pending instruction.** `achieved_plan_changes` closes the node with a
`plan_changes` route. Closing the last node would otherwise complete the goal before the architect
could read that instruction, since `_pending_finalizer_instructions` scans ACTIVE work objects only.
A satisfied goal therefore holds while any node carries an unconsumed finalizer instruction with a
`next_step`, until the architect consumes it. Only the automatic
rollup defers: the steward's explicit `set_work_status` stays authoritative, because a person
deciding a goal is over outranks a pending note about how to continue it.

**No re-planning, but always re-judge the moment.** A work node's precise time-wake opens
`dayflow_wake_manager`, a manager with no intake, evaluator or architect in its state_map — the
architect's decision about what to do, and roughly when, is not reopened, and there is nothing for
the pass to fall into if a routing step misfires. It does NOT skip the state_mover: whether right now
is a good moment is a fresh judgment every time, because the world moved since the timer was set (the
user went to bed early, the meeting ran long, they are away). The state_mover sees exactly that one
node; `work_node_wake_router_node` then dispatches it if it was left `actionable`, or ends the pass if
it was held. The hold persists `reactivate_at` as `wake_at`, but the next ordinary planning tick
must re-arm the timer: the wake pass currently performs no re-arm itself. A timed node is
ready BECAUSE its time came: the state_mover's prompt says a boundary announcement ("work hours are
over") is never held for the boundary it announces, and a hold moves by minutes, never to the next day.

**Crash recovery.** An ask is a tool call that outlives the process that made it: the thread waiting
on the user dies at shutdown, the QUESTION does not — it is a ticket row that may already carry the
answer. `work_session.re_arm_inflight_asks()` runs at boot, driven from the ticket side (the durable
record, which carries `trigger_context.work_node`). For each node still `dispatched` whose session is
gone: a recorded answer is landed as the result, a lapsed window lands "user not reached", and a
question still live is waited on for the REMAINDER of its window — never re-asked, because it is
still on screen. The newest ticket per node wins, a live session is never disturbed, and a timeout is
re-checked against the row before it is believed.

Every recovered result rejoins the normal completion path: `record_tool_result` saves the evidence
and result status, then the same `WorkFinalizerNode` used by the dispatch room judges it and persists
its verdict. Recovery supplies a fresh blackboard containing that work-node reference and passes the
dayflow room's scope to the finalizer. It signals planning after finalization; planning has no finalizer stage of its
own. If the recorder rejects a stale/ended call, recovery does not finalize its successor's result.

**Agents do not share a blackboard.** An agent's context items resolve from the blackboard it was
constructed with, so handing it one owned by something else silently rebinds every key they both use —
and `task` is the key every agent uses. `work_architect_node` was briefly built with the TICK's
blackboard so its context items would resolve; the tick's `task` is `"Dayflow cadence tick"`, which
then shadowed the goal. For eight hours the architect was asked to decompose the tick with the whole
portfolio as context, and wrote nodes for whatever it could see — a picture-day goal acquired an AC
setpoint, a lights ramp and an evening dog walk, two of which failed inside it and, under
`all_owned_children_done`, made the goal permanently unsatisfiable. An agent invoked from a control
node gets its own blackboard; what it needs arrives through the Message or as a resource.

### Still to do

- **`ManagerInterface._run_on_child_node`** still puts a work-graph special case inside the generic
  manager-as-tool wrapper. Removing it requires retiring the `node_aware` sub-manager variants
  (`work_web_manager`) in favour of plain ones, which changes what every run records in the graph.
- **A held wake needs the next planning tick to re-arm its timer.** `_fire_work_node` does not call
  `_arm_work_node_wakes`; a short hold can therefore run later than its requested `reactivate_at`.
- **The steward re-mints actions that already ran.** Its only defence against duplicate goals is a
  prompt telling it to check the portfolio. Routine actions that ran this morning were minted again
  as fresh work objects the same day.
- **The ROUTINE reads as a backlog.** It is timing context ("judge the goal's timing against this")
  but is written as imperatives — "Issue the cooling-stop action" — and the architect turns lines
  into nodes.
- **`abandon_work_ids` has no reason field**, so `work_persist` writes a hardcoded tautology on every
  abandon (448 of 592 historic rows). `propagate_work_outcome` then finds no user words, the
  originating concern stays active, and the goal is re-minted the next morning.
- **The failure ceiling counts per goal, not per action.** `goal_unmet_attempts` survives a re-plan
  but not the steward minting a fresh work object for the same action tomorrow. Catching that needs a
  counter keyed on something more durable — the action and its target, roughly what the `actions`
  ledger records.


## Current durability and prompt contracts (2026-09-19)

Admission persists an evaluator inbox across ticks using explicit `evaluator_pending=true` metadata. Legacy `triage_admit` classification alone does not enter this inbox. Pending admissions survive regardless of age; prompts display original source dates and do not treat historical deadlines as current urgency. New goals contain their source
summaries, pod handles and success criteria in the creation write; changes retain
those sources. Interrupted acknowledgments and empty-goal decomposition resume from
the durable store. Ingestion saves destination items before cursor updates, scans
inclusive source windows, and reserves identities across all retained intake.

Pending finalizer instructions block further main-task dispatch until the architect
commits its complete revision. Architect and steward share Jinja views of main tasks
and finalizer summaries. Worker/finalizer views include the owned provenance history.
See the [context contract and rendered example](../design/dayflow_prompt_context_standard_2026-09-19.md).

Ticket recovery requires an exact dispatch epoch. Historical tickets without one
are not guessed into a current attempt. Scheduler stop removes all owned jobs;
callbacks recheck stopped state and failed wakes delay retries by at least 120 seconds.
