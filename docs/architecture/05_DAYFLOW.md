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
| work_finalizer | what ONE call's outcome means (proceed / amend / replan / blocked) | sole producer of `closed`; `is_satisfied` keys on `closed`; `_STATUS_FOR` maps each verdict to one status |
| worker (`work_emi_team_manager`) | HOW a node gets done | render-loop manager; results land as evidence children |

`work_repair` was retired on 2026-09-16; its three dispositions moved to agents that see more than one
failed step (retry -> the finalizer's `replan`, which must NAME what will differ; escalate -> an ask is
just a node with a communicate goal, which the architect plans; abandon -> the steward's, which sees
every work object each run). Its files remain on disk, unwired — including
`work_repair_apply._MAX_ASK_TIMEOUTS`, so the 3-strike ask ceiling described below no longer fires.

## Architecture

```
DayflowScheduler (event-driven, debounced; precise per-node time wakes; work-progress follow-ups)
  -> dayflow_orchestrator_cadence_tick()
    -> run_dayflow_ingestion()          (chat / email / delegation / pods -> items table)
    -> three sweeps                     (dispatch_sweeper.py: stale / orphaned-dispatched / zombie-waiting)
    -> Invoke dayflow_orchestrator_manager (state_map order):
         tick_router -> intake_triage -> triage_persist -> context_enricher
           -> strategic_planner_wo (EVALUATOR) -> strategic_planner_wo_persist
           -> work_architect_node
           -> state_mover -> state_mover_persist (node promotion + event wakes)
           -> work_node_wake_router (a targeted time-wake dispatches here, or is held)
           -> work_node_materializer -> action_selector -> switchboard
           -> work_node_dispatch   (CLAIMS the node, opens the dispatch room, ENDS THE PASS)
           -> post_room_finalize -> final_answer

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
exclusively: the claim is an atomic `set_status -> dispatched` through the store's lock, so a second
room attempting the same node is refused. Exclusion lives on the work item, not on a global flag.

Cadence ticks remain mutually exclusive. Targeted work-node wakes do NOT go through that gate —
`_fire_work_node` invokes the orchestrator manager directly — which is safe because a targeted pass
routes `tick_router -> state_mover -> wake_router -> dispatch` and never plans.

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

**Decomposition.** The architect turns one goal into 1-5 subtask nodes under the goal node, with
`depends_on` edges and at most one wake primitive per node:
- `wake_at` — a deterministic time (absolute ISO datetime; elapsed time is always this),
- `wake_ref` — a prose external-event condition the state_mover matches against incoming intake.
A node whose goal is to reach the user is written plainly ("Tell/Ask the user X") — the switchboard
decides delivery, the architect never picks channels. On re-plan the architect emits a DELTA: new nodes
plus `abandon_node_ids` for moot branches (pruned recursively, finished nodes kept as a record).

**Readiness and promotion.** `is_ready` (substrate, deterministic) = status in
proposed/waiting/actionable + `wake_at` passed + all `depends_on` satisfied. The state_mover persist node promotes every ready node to
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

Ownership is a graph fact: the session stamps `payload.session_id` on the node, registry-first, so a
`dispatched` node with no live session is definitively orphaned rather than racing its own
registration. Each tick supervises the in-flight set (`sweep_stuck_work_nodes`): a node quiet for
longer than the longest legitimate call is failed, and the transition machine rejects a zombie thread's
late writes (`failed -> done` is illegal), so no torn state.

`node_dispatch.dispatch_node` still exists but is no longer on any path; the state_map plus
`open_session` is the dispatch core.

**Asks (user_reply).** An ask is a TOOL CALL whose result is the user's reply. Surfacing it creates the
ticket (validity window = the call's timeout, currently 1h; a new ask ticket expires prior open asks of
the same work object) and marks the node `dispatched + wake_kind=user_reply` — in flight, exactly like a
worker job; one live ask per work object (a second ask node queues behind it). The reply/dismissal is
matched back by `trigger_context.work_node`, recorded as an EVIDENCE child (the node's `content` is its
immutable directive), and the node completes -> the finalizer judges the reply like any result. An
unanswered ticket expiring is a TIMED-OUT call: the sweeper fails the node (reason appended to
`payload.status_notes` — never `content`, which is the immutable directive) and the finalizer judges
the failure. **The 3-strike bound no longer fires:** `_MAX_ASK_TIMEOUTS` / `ask_unanswered_ceiling`
live in `work_repair_apply`, which is unwired. `payload.failure_count` counts a node's failures and
warns the finalizer, architect and steward at two — but it counts PER NODE, and the steward mints a
fresh work object per occurrence, so an action that fails the same way every day never accumulates.
Ticket text is composed by `ticket_builder_manager`
(multi_agents/): dispatch hands over ids and the goal (`work_id::node_id` + the node's directive),
a read-only planner pulls the SUBSTANCE the goal promises — `read_work_object` on the graph,
`pod_fetch` to dereference pod ids (never pod_search, never anything world-facing) — and the
composer writes the final message. The message IS the delivery: a deliver-goal's ticket carries
the produced result itself, and if the graph provably lacks it, the ticket says so honestly
(loud failure reveals broken work). Self-contained briefs return_control at action 0, so the
common case costs one planner step. The store fence refuses terminal writes on an in-flight ask
(replan cannot prune a question that is out).
A repair-escalated ask (`proposed + user_reply`, no wake_at) promotes for its first surface via the
state_mover, which may HOLD it (a held pre-surface ask parks `waiting` and keeps `wake_kind=user_reply`
so a late reply to an earlier ticket still matches).

**Completion.** A worker-`done` node is only a RESULT. The finalizer runs in the DISPATCH ROOM, in the
same pass that made the call, and reads that node's full result. Four verdicts — two for a call that
returned, two for one that failed:

| | verdict | writes |
|---|---|---|
| returned | `proceed` | `done -> closed` (only `closed` counts toward the goal) |
| returned | `amend` | `done -> closed`, plus a revised intent for the architect |
| failed | `replan` | `failed -> proposed`, plus an instruction NAMING what must differ |
| failed | `blocked` | status unchanged; only the reason is recorded |

`replan` is deliberately rare: re-running a step whose circumstances have not changed reproduces its
error, so the contract refuses a `replan` that cannot name the difference. RESOLVE was removed on
2026-09-16 — a single node's result must not end a whole work object; a finalizer that thinks the goal
is finished or moot says so in `reasoning` and the steward rules next tick.

**Verdicts persist on the NODE**, not in memory: `nodes.payload.finalizer`, beside the terminal
epitaph. The architect that acts on an `amend` or `replan` runs on a LATER tick, and every tick builds
a fresh manager with a fresh blackboard, so anything left in memory is discarded before its reader
exists. `work_architect_node._pending_finalizer_instructions` reads them off the graph and
`consume_finalizer_instruction` stamps each one, so a judgment made ticks ago stops arriving as fresh
advice. When all of the goal's children are closed, the store's rollup completes the work object.

**Closure is a transition with obligations (2026-07-31).** Entering `done`/`abandoned` — via the
steward, finalizer, or repair — cascade-abandons every still-startable node
(proposed/actionable/waiting/failed) and clears its wakes, so a closed object can never fire again;
`WorkObject.validate()` enforces the invariant (a terminal object holding a startable node is a
write-time error), and `repair_terminal_zombies()` healed pre-cascade rows at boot. Motivation: a
`done` object's leftover `waiting` node kept an armed timer and ghost-ticketed the user a day later.
If the closed work object carries `concern:` provenance (`constraints.concern_refs`, forwarded from
the evaluator's `based_on`), the outcome — with the user's recorded words — back-propagates to the
subconscious concerns register and triggers a cooldown-guarded noticer rerun
(`subconscious/concern_feedback.py`, 2026-08-01).

**Failure.** A failed node is judged by the finalizer in the room that made the call: `replan` re-opens
it to the architect's inbox carrying what must be different, `blocked` leaves it failed with the reason
recorded for the steward. A failed node still blocks its goal (`is_satisfied` requires `closed`), so
the steward sees it loudly in the portfolio and decides what becomes of the goal. Dispatch errors mark
the node failed loudly rather than silently retrying.

## DayflowScheduler

`dayflow_scheduler.py` — event-driven with two-tier throttling:
- `DEBOUNCE_SECONDS=60`, `MIN_GAP_SECONDS=120` (mutual-exclusion floor), `POKE_MIN_INTERVAL_SECONDS=600`
  (delta pokes: chat/email/AFK/ticket), `MAX_CEILING_SECONDS=1800`, `STARTUP_TICK_DELAY_SECONDS=45`.
- **Precise work-node wakes**: one APScheduler one-shot per time-gated node (`dayflow_work_wake::` jobs,
  re-armed idempotently after every tick, restart-safe from the durable store). Firing is `is_ready`-gated
  and invokes the orchestrator manager directly (`_fire_work_node`) — outside the `_running` gate, so a
  targeted wake never waits on a cadence tick. It routes through the same state_mover and switchboard,
  so a node is judged and routed identically wherever it fires.
- **Ask recovery at boot**: `start()` calls `work_session.re_arm_inflight_asks()` before the first tick,
  so a question in flight when the process died is settled from its ticket rather than left orphaned.
- **Work-progress follow-up**: when a node reaches a result, a reply is recorded, or a dispatch leaves
  more ready nodes waiting, `dayflow_work_progress` schedules a prompt NON-poke tick (~MIN_GAP), so
  sequential chains and ready queues advance in minutes instead of one step per ceiling tick.
- **Item timers** still wake the scheduler for `waiting`/`watching` items (fast-tick promotes one item
  deterministically); ancient overdue items (>24h) are ignored as broken rather than hot-looping.
- **Failure escalation**: 3 consecutive tick failures surface one owner-visible ticket via the ticket
  manager's direct write (which does not run this pipeline).

## Persistent state — two substrates

**Work objects** (`work_objects/` substrate): four tables in emi.db (`work_objects`, `nodes`, `edges`,
`events`), opened via `dayflow_orchestrator/work_store.py`. The append-only event log is the source of
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

`create_dayflow_ticket` is a tool; ticket phrasing goes through the `ticket_builder` agent
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
silence cannot be the signal that a job died. `_WORK_NODE_FROZEN_TIMEOUT_S` is therefore DERIVED from
the longest legitimate tool call (the ask window) plus a grace, rather than guessed — it cannot drift
if either number moves. Crashes and restarts are caught by ORPHAN detection (no live thread), which
is immediate and does not wait for that timeout.

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
finalizer that thinks the goal is finished or moot says so in `reasoning`, and the steward rules on
it next tick.

**The rollup yields to a pending instruction.** `amend` and `replan` both close the node, and closing
the last one would otherwise complete the goal — destroying the instruction they just wrote, since
`_pending_finalizer_instructions` scans ACTIVE work objects only. So a satisfied goal holds while any
node carries an unconsumed finalizer instruction, until the architect consumes it. Only the automatic
rollup defers: the steward's explicit `set_work_status` stays authoritative, because a person
deciding a goal is over outranks a pending note about how to continue it.

**No re-planning, but always re-judge the moment.** A work node's precise time-wake fires a targeted
pass that skips intake, the evaluator, the architect and repair — the architect's decision about what
to do, and roughly when, is not reopened. It does NOT skip the state_mover: whether right now is a
good moment is a fresh judgment every time, because the world moved since the timer was set (the user
went to bed early, the meeting ran long, they are away). `work_node_wake_router_node` then dispatches
the node if the state_mover left it `actionable`, or ends the pass if it was held — in which case the
hold's `reactivate_at` re-arms the wake on its own. Until 2026-09-16 the targeted pass went straight
to the switchboard, so quiet-hours protection applied only to nodes that happened to arrive through a
planning tick: a 10pm reminder fired regardless, purely because it came through the timed door.

**Crash recovery.** An ask is a tool call that outlives the process that made it: the thread waiting
on the user dies at shutdown, the QUESTION does not — it is a ticket row that may already carry the
answer. `work_session.re_arm_inflight_asks()` runs at boot, driven from the ticket side (the durable
record, which carries `trigger_context.work_node`). For each node still `dispatched` whose session is
gone: a recorded answer is landed as the result, a lapsed window lands "user not reached", and a
question still live is waited on for the REMAINDER of its window — never re-asked, because it is
still on screen. The newest ticket per node wins, a live session is never disturbed, and a timeout is
re-checked against the row before it is believed.

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
- **The verdict set cannot say "the call returned but the goal did not happen."** `amend` is the
  closest fit and it maps to `closed`. Observed 2026-09-17: a lights goal whose own epitaph read
  "the result does not show that the lights were actually turned off". The goal no longer completes
  under an unconsumed instruction (below), so the architect now gets its say — but the node itself
  is still recorded as satisfied work.
- **The steward re-mints actions that already ran.** Its only defence against duplicate goals is a
  prompt telling it to check the portfolio. Routine actions that ran this morning were minted again
  as fresh work objects the same day.
- **The ROUTINE reads as a backlog.** It is timing context ("judge the goal's timing against this")
  but is written as imperatives — "Issue the cooling-stop action" — and the architect turns lines
  into nodes.
- **`abandon_work_ids` has no reason field**, so `work_persist` writes a hardcoded tautology on every
  abandon (448 of 592 historic rows). `propagate_work_outcome` then finds no user words, the
  originating concern stays active, and the goal is re-minted the next morning.
- **`work_repair` doctrine is still taught** in `work_architect/prompts/system.j2` ("Failed nodes are
  work_repair's ... repair's verdict reaches you next pass"), pointing at a retired agent.
- **The failure ceiling counts per node.** A recurring action that fails identically every day gets a
  fresh node each time, so `failure_count` never accumulates. Catching that needs a counter keyed on
  something more durable — the action and its target, roughly what the `actions` ledger records.
