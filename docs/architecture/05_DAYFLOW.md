# Dayflow Orchestrator

The Dayflow Orchestrator is an autonomous daily workflow engine that continuously manages the user's tasks, notifications, and proactive actions. It operates as a background "day planner AI."

## Core Philosophy

Every item has explicit lifecycle state. Nothing silently disappears; everything has a reason for its current state. User feedback is always respected. Actions are tracked and auditable.

Tickets, manager handoffs, tool calls — all forms of dispatch — go through the same path. There is no special two-phase ticket reconciliation. The room invocation owns the in-flight window; when it returns, the source item is closed.

## Architecture

```
DayflowScheduler (event-driven, debounced)
  -> dayflow_orchestrator_cadence_tick()
    -> run_dayflow_ingestion()         (chat / email / delegation / pods → items table)
    -> sweep_stale_dispatches()        ┐
    -> sweep_orphaned_dispatched_tasks ┤ dispatch_sweeper.py (three sweeps)
    -> sweep_zombie_waiting_items()    ┘
    -> Invoke dayflow_orchestrator_manager
      -> Deterministic pipeline (state_map order):
         tick_router -> intake_triage -> triage_persist -> context_enricher
           -> strategic_planner -> state_mover -> relevance_cleaner_gate
           -> view_materializer -> action_selector -> switchboard
           -> dayflow_switchboard_arguments -> dayflow_tool_caller
           -> action_result_normalizer -> post_room_finalize -> final_answer
      -> post_room_finalize_node persists state mutations + closes acted_on items
```

The pipeline is fixed by the manager's `state_map` (`multi_agents/dayflow_orchestrator_manager/config.yaml`), not by free agent handoffs. `relevance_cleaner` runs on a 30-minute gate: `relevance_cleaner_gate_node` either routes into `relevance_cleaner_prep_node -> relevance_cleaner -> relevance_cleaner_persist_node` or skips straight to `view_materializer_node`. A `fast_tick` wake (`tick_router -> fast_tick_promoter -> view_materializer`) bypasses triage/planner/state_mover for a single timer-driven item.

## DayflowScheduler

`app/assistant/dayflow_orchestrator/dayflow_scheduler.py`

Event-driven scheduling with intelligent debouncing. Two-tier wake throttle (constants atop the module):
- **Debouncing** (`DEBOUNCE_SECONDS=60`): multiple events within the window collapse into one run.
- **Item-tick floor** (`MIN_GAP_SECONDS=120`): mutual-exclusion floor for scheduled-item ticks; a job due at time T still fires at T.
- **Poke throttle** (`POKE_MIN_INTERVAL_SECONDS=600`): delta/poke wakes (chat/email/AFK/ticket) are held to ≥10 min since the last run, so dayflow doesn't react instantly to every event. Reaction = `max(debounce, floor - age)`, so the 10-min floor only bites when something *just* ran.
- **Ceiling tick** (`MAX_CEILING_SECONDS=1800`): a tick runs at least every 30 min when no item timers are pending.
- **Startup delay** (`STARTUP_TICK_DELAY_SECONDS=45`): the first tick after boot is delayed ~45s so it doesn't pile onto the post-restart routine/ingest fan-out and the user's first interaction.
- **Event subscriptions**: `repo_update` (only `email`/`calendar`/`todo_task`/`scheduler_events`), `afk_state_changed` (on return to `active`), `dayflow_ticket_responded` — each calls `poke()`.
- **Item timers**: `_schedule_next_from_items()` scans `waiting`/`watching` items for the earliest `reactivate_at_utc`; an item overdue by more than `ANCIENT_ITEM_OVERDUE_SECONDS` (24h) is ignored as broken-and-stuck rather than driving a 2-min reschedule loop. A timer wake carries `triggered_item_id` and becomes a `fast_tick`.
- **Mutual exclusion**: only one tick runs at a time; a poke arriving mid-run is queued and re-scheduled (throttled) at tick end.
- **Sooner-run guard**: `_schedule_tick` never pushes an already-scheduled, sooner run later — a throttled poke can't clobber a due item tick (the item tick re-ingests, so it absorbs the pending delta).
- **Repeated-failure escalation**: APScheduler swallows the re-raise, so `record_tick` tracks consecutive failures; at the 3rd in a row (`_FAILURE_NOTIFY_THRESHOLD`) `_maybe_notify_repeated_failure` raises one owner-visible `dayflow_notify` ticket via the ticket manager's direct write (which does NOT run the dayflow pipeline, so it works even when that pipeline is the thing failing).

## DayflowTick

`app/assistant/dayflow_orchestrator/dayflow_tick.py`

The heartbeat function `dayflow_orchestrator_cadence_tick()`:

1. **Master-room block check** — `orchestrator_status.py` persists a `blocked_until_utc` timestamp when the user chats in `master_room` (`block_dayflow_orchestrator_for_master_chat`, `MASTER_ROOM_BLOCK_SECONDS=180`). If the tick fires inside that window it records `last_skip_reason="blocked_by_master_room_timer"` and returns, so the user isn't talked over.
2. **Ingest** via `run_dayflow_ingestion()` (`ingestion.py`) — pulls new rows from each source and persists them as dayflow items. Four sources:
   - **Chat**: cross-room chat from `chat_ingestion_entitled_rooms` (ROOM.md `access:` block; currently `master_room`), watermarked by `chat_ingested_up_to_utc`.
   - **Email**: today's important emails from the event repository.
   - **Delegation**: `dayflow_request`-tagged messages from `master_room`.
   - **Pods** (`_ingest_pods`): pods from `pod_store` whose `(kind, source_kind)` matches the `ingestion_pod_kinds` allowlist (ROOM.md; currently Ring doorbell images), watermarked by `pods_ingested_up_to_utc`. Absent/empty allowlist = pod ingestion off.
   - All deduplicated by `item_id` against existing rows; `assign_short_ids` assigns LLM-facing numeric ids.
3. **Three sweeps** (`dispatch_sweeper.py`):
   - `sweep_stale_dispatches()` — closes in-flight `action_dispatch` rows past timeout (soft: no active manager invocation + >10 min; hard: >2h regardless), and revives the acted-on source item to `actionable` only if it is still `dispatched`.
   - `sweep_orphaned_dispatched_tasks()` — closes tasks stuck in `dispatched` >2h with no live dispatch row pointing at them (→ `closed`, not `actionable`; the planner re-mints from current context if the need is still alive).
   - `sweep_zombie_waiting_items()` — closes `waiting` items whose `reactivate_at_utc` is >36h past, i.e. aged out of the cleaner's 24h view (the cleaner gets first crack inside that window).
4. **Build minimal extras** via `build_dayflow_blackboard_extras()` — emits only `day_of_week`. Per-agent prep nodes load their own context (items, dispatches, etc.) off the items table.
5. **Invoke `dayflow_orchestrator_manager`** with a trigger Message carrying `wake_reason`, `fast_tick`, and `triggered_item_id`. The manager runs the pipeline (see Architecture above).

Per-agent prep nodes (e.g., `strategic_planner_prep_node`, `action_selector_prep_node`) call `get_dayflow_items()` and `dispatch_sweeper.list_active_dispatches()` directly at the point of use.

## State Machine

Canonical transitions live in `dayflow_item_writer.ALLOWED_TRANSITIONS`. `write_dayflow_item` validates every state change against this map; disallowed transitions raise `ValueError`. Idempotent no-op transitions (`X → X`, e.g. state_mover refreshing `reactivate_at` on a `waiting` item) are always allowed.

```
new ----------> artifact / needs_planning / important_open / actionable / suppressed
  +-> artifact ---------> needs_planning / important_open / actionable / watching / closed / suppressed / pending_directive
  +-> needs_planning ---> important_open / actionable / waiting / closed / suppressed
  +-> important_open ---> actionable / waiting / watching / dispatched / closed / suppressed / pending_directive
  +-> actionable -------> dispatched / waiting / watching / closed / suppressed / pending_directive
  +-> dispatched -------> closed / waiting / actionable / pending_directive
  +-> waiting ----------> actionable / dispatched / closed / suppressed
  +-> watching ---------> actionable / important_open / closed / suppressed
  +-> closed -----------> suppressed / actionable / pending_directive   (reopen / directive-revive)
  +-> suppressed -------> pending_directive   (narrow exception: a user directive references this artifact)
  +-> pending_directive -> actionable / dispatched / closed / suppressed
  +-> active -----------> closed / suppressed   (plan-synopsis lifecycle)
```

- **`pending_directive`** — the user replied to a ticket with a follow-up directive; the planner owes a decision (dispatch / ignore / escalate). Reachable from nearly every state, including a narrow re-entry from `suppressed` when a directive references a previously-rejected artifact, so `suppressed` is no longer strictly terminal.
- **`active`** — plan synopses (`source_type=plan_synopsis`, `build_plan_synopsis_dicts`), context-only guidance the planner authors; closes/suppresses only.

The dispatch-to-close path is owned by the room invocation. When the switchboard dispatches a tool, `dayflow_switchboard_arguments_node` stamps `state="dispatched"` on the acted-on item(s). When the tool returns, `post_room_finalize_node` reads `acted_on_item_ids` from the blackboard and writes `state="closed"` with `reason="action_completed"`.

### Canonical State Constants (`dayflow_item_writer.py`)

- `RESOLVED_STATES = {"closed", "suppressed"}` — item is done, action_selector ignores.
- `DONE_STATES = {"closed"}` — completed.
- `TERMINAL_STATES = {"suppressed"}` — permanently invisible to default queries.

`state_store.py` re-exports these for callers; do not redefine them.

## Sub-Agents

The directory `app/assistant/agents/dayflow_orchestrator/` holds **10 agent dirs**, but the manager (`multi_agents/dayflow_orchestrator_manager/config.yaml` `agents:`) only wires **7**: `intake_triage`, `strategic_planner`, `state_mover`, `relevance_cleaner`, `action_selector`, `switchboard`, `plan_mode`.

### Core Pipeline (wired in `state_map`)

| Agent | Purpose | Output |
|-------|---------|--------|
| `intake_triage` | Accept or reject new artifacts | ADMIT / REJECT_DUPLICATE / REJECT_NO_ACTION / REJECT_POLICY |
| `strategic_planner` | Create / maintain plans for goals | planned_tasks, plan_synopses |
| `state_mover` | Lifecycle transitions for important_open / waiting items | StateMutation records |
| `relevance_cleaner` | Close stale / completed items (30-minute gate) | close / suppress decisions |
| `action_selector` | Pick what to execute right now | acted_on_item_ids + action_type |
| `switchboard` | Delegate to manager / tool, or call `create_dayflow_ticket` itself | delegate_to, task, task_information |

### Supporting

| Agent | Purpose |
|-------|---------|
| `plan_mode` | Conversational planning agent (master-room delegation; `planning_mode` flow) |
| `result_formatter` | Compresses manager-result text after dispatch. **Not** a pipeline agent — invoked directly via `DI.agent_factory` in `post_room_finalize_node` and `master_room_tool_caller`. |
| `room_summary` | Compress orchestrator room conversation history (room chat-compaction) |

### Orphaned agent dirs

- `ticket_builder/` — superseded. Ticketing now flows `switchboard → create_dayflow_ticket` **tool** (`app/assistant/lib/tools/create_dayflow_ticket/`), dispatched like any other tool; there is no separate ticket-builder agent in the pipeline. The dir is dead code.

### Triage

`intake_triage` is a simple accept / reject gate. Each eligible artifact gets exactly one decision:
- `ADMIT` → state `artifact`, persisted by `triage_persist_node` for the planner to consider.
- `REJECT_*` → state `suppressed` with `reason="triage_<flavor>"`, also persisted by `triage_persist_node`. Suppressed is terminal so the rejected item exits the eligible bucket cleanly.

### State Mover

Processes items in `important_open` and `waiting` states only. Moves items to `actionable`, `waiting`, or `watching` based on timing and conditions. `state_transition_guard_node` validates each mutation against `ALLOWED_TRANSITIONS` and forbids targets in `_STATE_MOVER_FORBIDDEN_TARGETS = {closed}` and sources in `_STATE_MOVER_FORBIDDEN_SOURCES = {dispatched, closed}`.

### Action Selector

- Only acts on items in `actionable` state.
- At most one action per pass.
- Anti-duplication: checks active dispatches and recently completed.
- Respects user feedback (declined tickets stay declined via the suppressed state).

## Action Log

`state_store.write_action_log()` writes simple message entries to the DB:
- `event_type: "dispatch"` — task was dispatched to a manager / tool.
- `event_type: "result"` — manager / tool returned a result.

Each entry has `task_id` (short_id), `plan_id`, `summary`, `detail`. Entries are idempotent (deterministic id from `dispatch_id|event_type`).

The strategic planner and relevance cleaner see these in the "ACTION LOG (today)" section of their prompts.

## Persistent State

`app/assistant/dayflow_orchestrator/state_store.py` (read path) and `app/assistant/dayflow_orchestrator/dayflow_item_writer.py` (write path).

All dayflow items are `Message` objects stored in `unified_log_2026` with `source='dayflow_item'`:
- **Upsert key**: `Message.id` = `metadata.item_id`.
- **State field**: `metadata.state`.
- **Short IDs**: Numeric `short_id` persisted in metadata for LLM prompts.
- **Read accessors**: `get_dayflow_items()` (the agent-facing view), `_load_latest_dayflow_item_map()` / `load_existing_dayflow_items()` (raw, optionally terminal-inclusive).
- **Write paths**: `write_dayflow_item()` (singular, merges metadata, validates transition) and `write_dayflow_items_batch()` (bulk upsert).
- **Metadata access**: `get_meta(item)` for safe dict extraction.

**Freshness windows** (`get_dayflow_items`): suppressed items are excluded (terminal); **active items older than 24h** (`_MAX_AGE_HOURS`) and **closed items older than 2h** (`_CLOSED_MAX_AGE_HOURS`) are filtered out, keyed on `last_reviewed_at`. This is the "plans are same-day" behavior — an untouched item ages out of the agents' view after a day even though it remains in the DB (`load_existing_dayflow_items` still sees it). Action logs are written `closed` so they self-prune after 2h.

## Input Sources

`app/assistant/dayflow_orchestrator/ingestion.py` orchestrates ingestion. `input_message_builder.py` provides the per-source builders.

- **Chat**: `source_type='cross_room_chat'`, ingested as `state='closed'` (history-only).
- **Email**: `source_type='email'`, from event repository (importance >= 5), ingested as `state='artifact'`.
- **Delegation**: `source_type='user_request'`, from `master_room` chat_gate via `dayflow_request` tagged messages.
- **Pods**: built by `_build_pod_message` for pods matching the ROOM.md `ingestion_pod_kinds` allowlist (currently Ring doorbell images). Watermarked; first run caps at start-of-day so history isn't replayed.

Calendar events are NOT ingested as dayflow items. The planner sees them via `resource_expected_calendar` and creates plans from them directly.

Tickets are NOT ingested as dayflow items either. They are tool-call returns, owned by the room that dispatched them; their state lives in the ticket manager and their effect on dayflow items happens via the dispatching room's `acted_on_item_ids` at finalize.

## Chat Ingestion

`app/assistant/dayflow_orchestrator/chat_ingestion.py`

Cross-room chat ingested as context-only items:
- Entitled rooms defined in `access.json` (currently `["master_room"]`).
- Filters out noise (ticket, notification, system content types).
- Filters out non-normal room modes (task_creation, doc_creation, etc.).
- State always = `closed` (`reason="chat_ingested_for_history"`).

## Dispatch Provenance

`DayflowSwitchboardArgumentsNode` persists an `action_dispatch:UUID` item before each tool execution:
- Records: action type, arguments, acted-on item ids.
- Stamps `state="dispatched"` and `dispatched_at` on every acted-on item via `write_dayflow_item`.
- Injects `trigger_context.acted_on_item_ids` into the outgoing tool arguments so the tool can backlink.

After the tool returns, `post_room_finalize_node`:
- Validates `acted_on_item_ids` matches `active_dispatch_records`.
- Writes `state="closed"` + `reason="action_completed"` for each acted-on item.
- Persists `action_log` entries (dispatch + result) for the planner's "ACTION LOG (today)" section.

## Master Room Integration

- When the user chats in master_room, dayflow is blocked for 180 seconds (`MASTER_ROOM_BLOCK_SECONDS`).
- Master room chat_gate can delegate to dayflow (`dayflow_delegate_tf=true`).
- Delegations write a tagged request (`source='dayflow_request'`) that `run_dayflow_ingestion()` picks up on the next tick.
- Dayflow ingests master_room chat as context items.

## Key Files

| File | Purpose |
|------|---------|
| `dayflow_orchestrator/dayflow_tick.py` | Cadence tick entry point |
| `dayflow_orchestrator/dayflow_scheduler.py` | Event-driven scheduling |
| `dayflow_orchestrator/ingestion.py` | Per-source ingestion driver |
| `dayflow_orchestrator/state_store.py` | Read accessors, action log writer |
| `dayflow_orchestrator/dayflow_item_writer.py` | Single write path, ALLOWED_TRANSITIONS, RESOLVED_STATES |
| `dayflow_orchestrator/input_message_builder.py` | Per-source Message builders (email, delegation, pod) |
| `dayflow_orchestrator/orchestrator_status.py` | Status resource CRUD, master-room block timer, ingest watermarks |
| `dayflow_orchestrator/blackboard_builder.py` | Emits day_of_week (per-agent prep nodes own the rest) |
| `dayflow_orchestrator/dispatch_sweeper.py` | Three tick sweeps (stale / orphaned-dispatched / zombie-waiting) + active-dispatch listing |
| `dayflow_orchestrator/chat_ingestion.py` | Cross-room chat ingestion |
| `dayflow_orchestrator/contracts.py` | Pydantic validation, get_meta(), short_id utilities |
| `control_nodes/tick_router_node.py` | Top-of-pipeline router: `fast_tick` → promoter, else full pipeline |
| `control_nodes/triage_spawn_guard_node.py` | Validates triage decisions, mutates in-memory |
| `control_nodes/triage_persist_node.py` | Persists ADMIT and REJECT decisions |
| `control_nodes/state_transition_guard_node.py` | Validates and persists state_mover mutations |
| `control_nodes/dayflow_switchboard_arguments_node.py` | Pre-dispatch provenance, stamps dispatched state |
| `control_nodes/post_room_finalize_node.py` | Post-dispatch finalization, closes acted_on items, runs result_formatter |
| `control_nodes/relevance_cleaner_gate_node.py` | 30-minute gate for cleaner runs |
| `control_nodes/view_materializer_node.py` | Build agent-facing item views (filters items already covered by an in-flight dispatch) |
| `lib/tools/create_dayflow_ticket/` | The ticket tool the switchboard dispatches (replaces the old ticket_builder agent) |
| `rooms/dayflow_orchestrator/ROOM.md` | Room config (authority 95, `ingestion_pod_kinds`, `chat_ingestion_entitled_rooms`) |
| `agents/dayflow_orchestrator/` | 10 agent dirs; 7 wired in the pipeline (ticket_builder orphaned; result_formatter/room_summary out-of-pipeline) |
