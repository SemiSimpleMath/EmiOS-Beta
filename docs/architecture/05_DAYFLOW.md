# Dayflow Orchestrator

The Dayflow Orchestrator is the autonomous daily workflow engine — a background "day planner AI" that
continuously turns intake (email, chat, calendar-driven routine, delegations) into executed work,
reminders, and questions for the user.

## Core Philosophy

**Everything actionable is a WORK OBJECT** — a goal plus a small DAG of typed nodes in a durable,
event-sourced graph store. Even a one-shot action is a one-node work object. Items (the older Message
substrate) are intake and context only; the evaluator is the sole path from intake to action.

Every graph mutation goes through a validated writer (allowed transitions per node family, authority
ceilings, atomic event + projection). Nothing silently disappears: results are recorded as evidence,
failures surface loudly for adjudication, and user feedback is authoritative at every layer.

Sharp role separation, one bounded LLM judgment per role, deterministic mechanics everywhere else:

| Role | Decides | Deterministic guard |
|------|---------|---------------------|
| evaluator (`strategic_planner_wo`) | WHAT work exists (create/change/re-plan/complete/abandon) | `work_persist` applies; intake consumed with provenance |
| work_finalizer | whether a completed node's RESULT satisfies its step | sole producer of `closed`; `is_satisfied` keys on `closed` |
| work_architect | the STRUCTURE of one goal (DAG + wake gates) | `work_architect_apply` projects the delta |
| work_repair | disposition of a FAILED node (escalate/retry/abandon) | `work_repair_apply`, surgical re-open |
| state_mover | HOLD-only social timing (quiet hours, meetings, away) | promotion itself is deterministic `is_ready` (time + deps) |
| switchboard | WHERE one ready node goes, by READING its goal | two routes only: ticket the user, or run the worker |
| worker (`work_emi_team_manager`) | HOW a node gets done | render-loop manager; results land as evidence children |

## Architecture

```
DayflowScheduler (event-driven, debounced; precise per-node time wakes; work-progress follow-ups)
  -> dayflow_orchestrator_cadence_tick()
    -> run_dayflow_ingestion()          (chat / email / delegation / pods -> items table)
    -> three sweeps                     (dispatch_sweeper.py: stale / orphaned-dispatched / zombie-waiting)
    -> Invoke dayflow_orchestrator_manager (state_map order):
         tick_router -> intake_triage -> triage_persist -> context_enricher
           -> strategic_planner_wo (EVALUATOR) -> strategic_planner_wo_persist
           -> work_finalizer_node -> work_architect_node -> work_repair_node
           -> state_mover -> state_transition_guard -> state_mover_persist (node promotion + event wakes)
           -> work_node_materializer -> action_selector -> switchboard -> work_node_dispatch
                (ONE dispatch per tick; the worker detaches onto its own job thread)
           -> post_room_finalize -> final_answer
```

The pipeline is fixed by the manager's `state_map`
(`multi_agents/dayflow_orchestrator_manager/config.yaml`), not by free agent handoffs. The
materializer/selector/switchboard/dispatch segment dispatches ONE node per tick: the worker runs on its
own job thread and the tick proceeds to finalize; when more ready nodes remain, the work-progress
follow-up brings the next tick in minutes. Ticks stay mutually exclusive (one planning pass at a time) —
concurrency comes from job threads overlapping ACROSS ticks, with every pass seeing the in-flight state
(`dispatched` nodes are structurally excluded from the ready list).

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
  recipient) -> `run_work_node` — the worker (`work_emi_team_manager`) picks its own sub-managers/tools.
`node_dispatch.dispatch_node` is the single dispatch core, shared by the tick loop and the scheduler's
off-tick time wakes, so a node routes identically wherever it fires. Worker dispatch is ONE THREAD PER
OPEN TASK: the node is claimed (`dispatched`) synchronously, the worker runs on a detached job thread,
and the graph is the return channel. Each tick supervises the in-flight jobs
(`sweep_stuck_work_nodes`): an orphaned job (restart / thread death) or a frozen one (no subtree/job
activity for 20+ min) fails the node for work_repair — and the transition machine rejects a zombie
thread's late writes (`failed -> done` is illegal), so no torn state.

**Asks (user_reply).** A ticketed node parks `waiting + wake_kind=user_reply + wake_at=<re-ask time>`
(currently +1h). The reply is matched back by `trigger_context.work_node`, recorded as an EVIDENCE child
(the node's `content` is its immutable directive), and the node completes -> the finalizer judges the
reply like any result. An unanswered ask re-promotes when its re-ask time passes and is re-ticketed; the
state_mover can hold re-asks during quiet hours (a held ask keeps `wake_kind=user_reply` so a reply
still matches). A repair-escalated ask (`proposed + user_reply`, no wake_at) promotes for its first
surface the same way. There is no one-way ticket — every ticket awaits a response.

**Completion.** A worker-`done` node is only a RESULT. The finalizer (runs BEFORE the architect, so an
AMEND re-plans same-tick) reads the node's full result against the whole work object and emits one
verdict: PROCEED (close it — only `closed` counts toward the goal), AMEND (close + re-plan with a
revised intent), or RESOLVE (the goal itself is settled: complete or abandon the work object). When all
of the goal's children are closed, the store's rollup completes the work object.

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

**Failure.** A failed node blocks its goal until work_repair adjudicates: ESCALATE (re-issue as an ask —
the user does/provides what the assistant cannot), RETRY (transient, or the needed info arrived), or
ABANDON_GOAL (declines are authoritative and override the prefer-escalate bias). Dispatch errors mark
the node failed loudly rather than silently retrying.

## DayflowScheduler

`dayflow_scheduler.py` — event-driven with two-tier throttling:
- `DEBOUNCE_SECONDS=60`, `MIN_GAP_SECONDS=120` (mutual-exclusion floor), `POKE_MIN_INTERVAL_SECONDS=600`
  (delta pokes: chat/email/AFK/ticket), `MAX_CEILING_SECONDS=1800`, `STARTUP_TICK_DELAY_SECONDS=45`.
- **Precise work-node wakes**: one APScheduler one-shot per time-gated node (`dayflow_work_wake::` jobs,
  re-armed idempotently after every tick, restart-safe from the durable store). Firing is `is_ready`-gated
  and routes through the SAME switchboard as tick dispatch (`route_and_dispatch`).
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

**Legacy item lane (retirement, step C).** The item dispatch path (view_materializer -> action_selector
with items) survives only on fast-tick/plan flows and is instrumented: anything reaching the selector
logs `LEGACY ITEM LANE fired`, and an item reaching the work dispatch is closed loudly
(`legacy_item_lane_dispatch_retired`) — the evaluator should have converted it. The relevance_cleaner
was retired at the cutover (its files remain, unwired). Full lane deletion is pending a dormancy
observation window.

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
| `dayflow_orchestrator/work_architect_apply.py` | Projects an architect DAG/delta onto the graph |
| `dayflow_orchestrator/work_repair_apply.py` | Applies a repair disposition |
| `dayflow_orchestrator/work_portfolio.py` | Strategic projection (failures loud, outcomes as node -> result) |
| `dayflow_orchestrator/node_dispatch.py` | The single dispatch core (ticket vs worker) + progress signal |
| `dayflow_orchestrator/state_store.py` / `dayflow_item_writer.py` | Item substrate read / validated write |
| `dayflow_orchestrator/dispatch_sweeper.py` | Tick sweeps (stale / orphaned / zombie) |
| `control_nodes/strategic_planner_wo_prep/persist_node.py` | Evaluator context build / output apply |
| `control_nodes/work_finalizer_node.py` | done -> closed reconciliation (sole closer) |
| `control_nodes/work_architect_node.py` | Decompose new goals, re-plan flagged ones |
| `control_nodes/work_repair_node.py` | Failed-node adjudication |
| `control_nodes/state_mover_prep/persist_node.py` | Waits + promotion candidates / promotion + node wakes |
| `control_nodes/work_node_materializer_node.py` | Ready-node listing + reply pre-step |
| `control_nodes/work_node_dispatch_node.py` | Carries out the switchboard's routing (+ legacy-item guard) |
| `work_objects/model.py` / `store.py` | Substrate: graph model, validated writer, transitions |
| `work_objects/work_runtime.py` | `work_on`/`run_node` — drives one node through the worker manager |
| `multi_agents/work_emi_team_manager/config.yaml` | The worker: render-loop manager (DESIGN.md §4) |
| `agents/dayflow_orchestrator/` | The pipeline agents (evaluator, architect, switchboard, finalizer, repair, ...) |
