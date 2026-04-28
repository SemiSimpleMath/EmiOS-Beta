# Dayflow Orchestrator

The Dayflow Orchestrator is an autonomous daily workflow engine that continuously manages the user's tasks, notifications, and proactive actions. It operates as a background "day planner AI."

## Core Philosophy

Every item has explicit lifecycle state. Nothing silently disappears; everything has a reason for its current state. User feedback is always respected. Actions are tracked and auditable.

Tickets, manager handoffs, tool calls — all forms of dispatch — go through the same path. There is no special two-phase ticket reconciliation. The room invocation owns the in-flight window; when it returns, the source item is closed.

## Architecture

```
DayflowScheduler (event-driven, debounced)
  -> dayflow_orchestrator_cadence_tick()
    -> run_dayflow_ingestion()        (chat / email / delegation → items table)
    -> sweep_stale_dispatches()       (revive items whose dispatch never returned)
    -> Invoke dayflow_orchestrator_manager
      -> Multi-agent pipeline:
        1. intake_triage      (admit / reject new artifacts)
        2. strategic_planner  (create / maintain plans)
        3. state_mover        (lifecycle transitions)
        4. relevance_cleaner  (close stale / completed items, every 30 min)
        5. action_selector    (pick what to execute)
        6. ticket_builder     (when ticketing)
        7. switchboard        (delegate to manager / tool)
      -> post_room_finalize_node persists state mutations + closes acted_on items
```

## DayflowScheduler

`app/assistant/dayflow_orchestrator/dayflow_scheduler.py`

Event-driven scheduling with intelligent debouncing:
- **Debouncing**: Multiple events within 60s trigger a single run.
- **Minimum gap**: At least 120s between consecutive runs.
- **Event subscriptions**: `repo_update`, `afk_state_changed`, `dayflow_ticket_responded`.
- **Item timers**: Scans for `reactivate_at_utc` and schedules the next run accordingly.
- **Ceiling tick**: Runs every 30 minutes if no events pending.
- **Mutual exclusion**: Only one tick runs at a time.

## DayflowTick

`app/assistant/dayflow_orchestrator/dayflow_tick.py`

The heartbeat function `dayflow_orchestrator_cadence_tick()`:

1. **Master-room block check** — if `master_room` recently saw activity, skip this tick (180-second window) so the user isn't talked over.
2. **Ingest** via `run_dayflow_ingestion()` (`ingestion.py`) — pulls new rows from each source and persists them as dayflow items:
   - Chat: cross-room chat history within the entitled rooms (`access.json`).
   - Email: today's important emails from the event repository.
   - Delegation: tagged messages from `master_room` flagged for the orchestrator.
   - All deduplicated by `item_id` against existing rows; `assign_short_ids` assigns LLM-facing numeric ids.
3. **Sweep stale dispatches** via `sweep_stale_dispatches()` — for any `dispatched` item whose room invocation never returned, revives the source item to `actionable`.
4. **Build minimal extras** via `build_dayflow_blackboard_extras()` — emits only `day_of_week`. Per-agent prep nodes load their own context (items, dispatches, etc.) off the items table.
5. **Invoke `dayflow_orchestrator_manager`** with a trigger Message. The manager runs the agent pipeline (see Architecture above).

Per-agent prep nodes (e.g., `strategic_planner_prep_node`, `action_selector_prep_node`) call `get_dayflow_items()` and `dispatch_sweeper.list_active_dispatches()` directly at the point of use.

## State Machine

Canonical transitions live in `dayflow_item_writer.ALLOWED_TRANSITIONS`. `write_dayflow_item` validates every state change against this map; disallowed transitions raise `ValueError`.

```
new ----------> artifact         (context-only, e.g. emails, reference material)
  |             important_open   (admitted by triage, planner decides next)
  |             needs_planning   (planner-flagged for plan creation)
  |             actionable       (ready for action_selector)
  |             suppressed       (triage rejected)
  |
  +-> artifact -----> needs_planning / important_open / actionable / watching / closed / suppressed
  +-> important_open -> actionable / waiting / watching / dispatched / closed / suppressed
  +-> actionable ---> dispatched / waiting / watching / closed / suppressed
  +-> dispatched ---> closed / waiting / actionable
  +-> waiting ------> actionable / dispatched / closed / suppressed
  +-> watching -----> actionable / important_open / closed / suppressed
  +-> closed -------> actionable / suppressed     (reopen allowed)
  +-> suppressed ---> (terminal)
```

The dispatch-to-close path is owned by the room invocation. When the switchboard dispatches a tool, `dayflow_switchboard_arguments_node` stamps `state="dispatched"` on the acted-on item(s). When the tool returns, `post_room_finalize_node` reads `acted_on_item_ids` from the blackboard and writes `state="closed"` with `reason="action_completed"`.

### Canonical State Constants (`dayflow_item_writer.py`)

- `RESOLVED_STATES = {"closed", "suppressed"}` — item is done, action_selector ignores.
- `DONE_STATES = {"closed"}` — completed.
- `TERMINAL_STATES = {"suppressed"}` — permanently invisible to default queries.

`state_store.py` re-exports these for callers; do not redefine them.

## Sub-Agents

Located in `app/assistant/agents/dayflow_orchestrator/`. There are 9 agents:

### Core Pipeline

| Agent | Purpose | Output |
|-------|---------|--------|
| `intake_triage` | Accept or reject new artifacts | ADMIT / REJECT_DUPLICATE / REJECT_NO_ACTION / REJECT_POLICY |
| `strategic_planner` | Create / maintain plans for goals | planned_tasks, plan_synopses |
| `state_mover` | Lifecycle transitions for important_open / waiting items | StateMutation records |
| `relevance_cleaner` | Close stale / completed items (30-minute gate) | close / suppress decisions |
| `action_selector` | Pick what to execute right now | acted_on_item_ids + action_type |
| `ticket_builder` | Transform intent into ticket fields | ticket_kind, title, message |
| `switchboard` | Delegate to manager / tool | delegate_to, task, task_information |

### Supporting

| Agent | Purpose |
|-------|---------|
| `room_summary` | Compress orchestrator room conversation history |
| `plan_mode` | Conversational planning agent (master-room delegation) |

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
- **Read accessors**: `get_dayflow_items()`, `_load_latest_dayflow_item_map()`.
- **Write paths**: `write_dayflow_item()` (singular, merges metadata, validates transition) and `write_dayflow_items_batch()` (bulk upsert).
- **Metadata access**: `get_meta(item)` for safe dict extraction.

## Input Sources

`app/assistant/dayflow_orchestrator/ingestion.py` orchestrates ingestion. `input_message_builder.py` provides the per-source builders.

- **Chat**: `source_type='cross_room_chat'`, ingested as `state='closed'` (history-only).
- **Email**: `source_type='email'`, from event repository (importance >= 5), ingested as `state='artifact'`.
- **Delegation**: `source_type='user_request'`, from `master_room` chat_gate via `dayflow_request` tagged messages.

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
| `dayflow_orchestrator/input_message_builder.py` | Per-source Message builders (email, delegation) |
| `dayflow_orchestrator/blackboard_builder.py` | Emits day_of_week (per-agent prep nodes own the rest) |
| `dayflow_orchestrator/dispatch_sweeper.py` | Revives stuck-dispatched items |
| `dayflow_orchestrator/chat_ingestion.py` | Cross-room chat ingestion |
| `dayflow_orchestrator/contracts.py` | Pydantic validation, get_meta(), short_id utilities |
| `control_nodes/triage_spawn_guard_node.py` | Validates triage decisions, mutates in-memory |
| `control_nodes/triage_persist_node.py` | Persists ADMIT and REJECT decisions |
| `control_nodes/state_transition_guard_node.py` | Validates and persists state_mover mutations |
| `control_nodes/dayflow_switchboard_arguments_node.py` | Pre-dispatch provenance, stamps dispatched state |
| `control_nodes/post_room_finalize_node.py` | Post-dispatch finalization, closes acted_on items |
| `control_nodes/relevance_cleaner_gate_node.py` | 30-minute gate for cleaner runs |
| `control_nodes/view_materializer_node.py` | Build agent-facing item views |
| `rooms/dayflow_orchestrator/` | Room config |
| `agents/dayflow_orchestrator/` | Sub-agents (9 agents) |
