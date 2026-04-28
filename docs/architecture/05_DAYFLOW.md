# Dayflow Orchestrator

The Dayflow Orchestrator is an autonomous daily workflow engine that continuously manages the user's tasks, notifications, and proactive actions. It operates as a background "day planner AI."

## Core Philosophy

Every item has explicit lifecycle state. Nothing silently disappears; everything has a reason for its current state. User feedback is always respected. Actions are tracked and auditable.

## Architecture

```
DayflowScheduler (event-driven, debounced)
  -> dayflow_orchestrator_cadence_tick()
    -> Build context (items, tickets, emails, health, chat)
    -> Apply ticket feedback (stamp responses onto source tasks)
    -> Enrich with local times + short_ids
    -> Invoke room session for dayflow_orchestrator room
      -> Multi-agent pipeline:
        1. intake_triage    (accept/reject new items)
        2. strategic_planner (create/maintain plans)
        3. state_mover      (lifecycle transitions)
        4. relevance_cleaner (close stale items, every 30 min)
        5. view_materializer (build agent-facing views)
        6. action_selector  (pick what to execute)
        7. ticket_builder   (if ticket_tf=true)
        8. switchboard      (if handoff_tf=true)
      -> Persist updated items + dispatch records
```

## DayflowScheduler

`app/assistant/dayflow_orchestrator/dayflow_scheduler.py`

Event-driven scheduling with intelligent debouncing:
- **Debouncing**: Multiple events within 60s trigger a single run
- **Minimum gap**: At least 120s between consecutive runs
- **Event subscriptions**: `repo_update`, `afk_state_changed`, `dayflow_ticket_responded`
- **Item timers**: Scans for `reactivate_at_utc` and schedules next run
- **Ceiling tick**: Runs every 30 minutes if no events pending
- **Mutual exclusion**: Only one tick runs at a time

## DayflowTick

`app/assistant/dayflow_orchestrator/dayflow_tick.py`

The heartbeat function `dayflow_orchestrator_cadence_tick()`:

1. **Load context** via `_build_dayflow_blackboard_extras()`:
   - Existing dayflow items from unified_log_2026 (all states, staleness-gated)
   - Active tickets (PENDING, PROPOSED, SNOOZED)
   - Today's important emails (importance >= 5, ingested as `state: artifact`)
   - User delegation requests from master_room (tagged messages awaiting triage)
   - Health status summary, current location
   - Recent ticket responses (last 2 hours)
   - Cross-room chat history
   - Plan synopses and per-plan task status
   - Action log entries (dispatch/result messages)

2. **Apply ticket feedback** (`ticket_feedback.py`):
   - Ticket acceptance → patches `execution_result` + state = `acted_on` on source task
   - Ticket decline → state = `suppressed`
   - Ticket snooze → state = `waiting` + `reactivate_at`
   - Uses `patch_item_metadata()` to merge fields without full-row rewrite

3. **Enrich items**:
   - Human-readable local times and relative durations
   - Assign stable short_ids (monotonic counter, 1-10000) for LLM-facing prompts
   - Short_ids only assigned to non-resolved items (closed/suppressed/acted_on excluded)

4. **Invoke room session** for `dayflow_orchestrator` room

## State Machine

```
new ---------> artifact (context-only: emails, reference material)
  |
  +----------> important_open (admitted by triage, planner decides next)
  |
  +----------> actionable (ready for action_selector to execute)
                  |
                  +----> dispatched (manager executing the task)
                  |
                  +----> watching (passive monitoring)
                  |
                  +----> waiting (blocked on time/event/dependency)
                  |
                  +----> acted_on (execution complete, result attached)
                              |
                              +----> suppressed (user rejected or cleaner dismissed)
                              |
                              +----> closed (terminal, ages out via staleness gate)
```

### Canonical State Constants (`state_store.py`)

- `RESOLVED_STATES = {"acted_on", "suppressed", "closed"}` — item is done
- `DONE_STATES = {"acted_on", "closed"}` — completed successfully
- `TERMINAL_STATES = {"closed"}` — will never change again

Items age out of prompts via the staleness gate (12 hours). Closed/suppressed items are always subject to the staleness gate regardless of source type.

## Metadata Patching

State mutations and field updates use `patch_item_metadata()` which reads the current metadata from the DB, merges only the changed keys, and writes back. This prevents full-row rewrites that could clobber fields set by other nodes in the same tick.

`apply_state_mutations()` uses `patch_item_metadata()` internally and also updates the in-memory item dict for downstream nodes.

## Sub-Agents

Located in `app/assistant/agents/dayflow_orchestrator/`:

### Core Pipeline

| Agent | Purpose | Output |
|-------|---------|--------|
| `intake_triage` | Accept or reject new items (simple gate) | ADMIT or REJECT_* decisions |
| `strategic_planner` | Create/maintain plans for goals | planned_tasks, plan_synopses |
| `state_mover` | Lifecycle transitions (important_open → actionable/waiting/watching) | StateMutation records |
| `relevance_cleaner` | Close stale/completed items (runs every 30 min) | close/suppress decisions |
| `action_selector` | Pick what to execute right now | no_op, ticket, or handoff |
| `ticket_builder` | Transform intent into ticket fields | ticket_kind, title, message |
| `switchboard` | Delegate to manager/tool | delegate_to, task, reason |

### Supporting

| Agent | Purpose |
|-------|---------|
| `room_summary` | Compress orchestrator room conversation history |

### Triage

Triage is a simple accept/reject gate. All admitted items enter as `state: artifact`. The strategic planner decides which artifacts need plans and creates tasks from them. Triage does not distinguish between "needs planning" and "context only" — that's the planner's job.

### State Mover

Processes items in `important_open` and `waiting` states only. Moves items to `actionable`, `waiting`, or `watching` based on timing and conditions. Never touches `dispatched`, `acted_on`, or `closed` items — the state transition guard node drops any such mutations.

### Action Selector

- Only acts on items in `actionable` state
- Anti-duplication: checks active dispatches, active tickets, recently completed
- Respects user feedback (declined tickets stay declined)
- At most one action per pass

## Action Log

`state_store.write_action_log()` writes simple message entries to the DB:
- `event_type: "dispatch"` — task was dispatched to a manager
- `event_type: "result"` — manager returned a result
- `event_type: "ticket_response"` — user responded to a ticket

Each entry has `task_id` (short_id), `plan_id`, `summary`, `detail`. Entries are idempotent (deterministic ID from `task_id|event_type|ticket_id`).

The strategic planner and relevance cleaner see these in the "ACTION LOG (today)" section of their prompts.

## Persistent State

`app/assistant/dayflow_orchestrator/state_store.py`

All dayflow items are `Message` objects stored in `unified_log_2026` with `source='dayflow_item'`:
- **Upsert key**: `Message.id` = `metadata.item_id`
- **State field**: `metadata.state`
- **Short IDs**: Numeric `short_id` persisted in metadata for LLM prompts
- **Metadata access**: `get_meta(item)` utility for safe dict extraction
- **Metadata updates**: `patch_item_metadata(item_id, updates)` for merge-not-overwrite
- **Reloading**: `reload_active_items_onto_blackboard()` shared by all persist nodes

## Input Sources

`app/assistant/dayflow_orchestrator/input_message_builder.py`

Normalizes heterogeneous inputs into uniform Message payloads:
- **Tickets**: `source_type='ticket'`, from ticket manager (active states only)
- **Emails**: `source_type='email'`, from event repository (importance >= 5), ingested as `state: artifact`
- **User delegations**: `source_type='user_request'`, from master_room chat_gate via `dayflow_request` tagged messages in the DB

Calendar events are NOT ingested as dayflow items. The planner sees them via `resource_expected_calendar` and creates plans from them directly.

## Chat Ingestion

`app/assistant/dayflow_orchestrator/chat_ingestion.py`

Cross-room chat ingested as context-only items:
- Entitled rooms defined in `access.json` (currently `["master_room"]`)
- Filters out noise (ticket, notification, system content types)
- Filters out non-normal room modes (task_creation, doc_creation, etc.)
- State always = `closed` ("chat_ingested_for_history")

## Dispatch Provenance

`DayflowSwitchboardArgumentsNode` persists an `action_dispatch:UUID` item before each execution:
- Records: action type, arguments, acted-on item IDs
- Stamps `dispatched_at` on acted-on plan tasks via `patch_item_metadata()`
- Writes action log messages for dispatch and result

## Master Room Integration

- When user chats in master_room, dayflow is blocked for 180 seconds
- Master room chat_gate can delegate to dayflow (`dayflow_delegate_tf=true`)
- Delegations write a tagged request (`source='dayflow_request'`) that the intake pipeline picks up on the next tick
- Dayflow ingests master_room chat as context items

## Key Files

| File | Purpose |
|------|---------|
| `dayflow_orchestrator/dayflow_tick.py` | Main orchestrator loop |
| `dayflow_orchestrator/dayflow_scheduler.py` | Event-driven scheduling |
| `dayflow_orchestrator/state_store.py` | Persistent state, patch_item_metadata, write_action_log |
| `dayflow_orchestrator/input_message_builder.py` | Input normalization (tickets, emails, delegations) |
| `dayflow_orchestrator/blackboard_builder.py` | Context loading (~950 lines) |
| `dayflow_orchestrator/ticket_feedback.py` | Stamp ticket responses onto source tasks |
| `dayflow_orchestrator/chat_ingestion.py` | Cross-room chat ingestion |
| `dayflow_orchestrator/contracts.py` | Pydantic validation, get_meta(), short_id utilities |
| `control_nodes/state_transition_guard_node.py` | Validates and persists state mutations |
| `control_nodes/dayflow_switchboard_arguments_node.py` | Dispatch provenance |
| `control_nodes/relevance_cleaner_gate_node.py` | 30-minute gate for cleaner runs |
| `control_nodes/view_materializer_node.py` | Build agent-facing item views |
| `rooms/dayflow_orchestrator/` | Room config |
| `agents/dayflow_orchestrator/` | Sub-agents |
