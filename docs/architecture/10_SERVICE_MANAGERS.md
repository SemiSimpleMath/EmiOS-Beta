# Service Managers

The "Manager" suffix is overloaded — see [02_MANAGERS](02_MANAGERS.md)
for the agent-orchestrating side. This page covers the *other* kind:
long-lived services that share the suffix as a naming convention but
have nothing else in common with `MultiAgentManager`. They never run
an agent loop, never use a `Blackboard`, never have a manager YAML
config, and aren't invoked through `ManagerInvoker`.

If you're looking for how DI is wired or what's in the registry, see
[17_SERVICE_LAYER](17_SERVICE_LAYER.md). This page is about each
service manager's responsibility — the *what*, not the *how-it-gets-injected*.

## RoutineManager

Source: `app/assistant/routine_manager/routine_manager.py`.

Schedules and dispatches routine work from `configs/routines.json`.

- **5 runner types**: `task`, `job`, `tool`, `function`, `pipeline`.
- **5 scheduling policies**: `interval`, `daily`, `weekly`, `quiet_hours`,
  plus AFK/manual/feature guards.
- **State persistence**: thread-safe writes to
  `resource_routine_status.json` so last-run timestamps survive restarts.
- **Worker pool**: fixed-capacity (`max_workers` in config).

Function-type routines are registered in
`app/assistant/routine_manager/routine_functions.py` under
`ROUTINE_FUNCTION_REGISTRY`. Pipeline-type routines invoke a pipeline
class's `.run()` directly. Neither runs an LLM agent loop.

The `BackgroundTaskManager`'s `routine_runner` daemon task is what
actually wakes RoutineManager on a clock — RoutineManager itself
doesn't own a thread.

See [06_PIPELINES_AND_ROUTINES](06_PIPELINES_AND_ROUTINES.md) for the
pipeline contract and [20_ROUTINES_ADMIN](20_ROUTINES_ADMIN.md) for
the `/routines` admin UI.

## BackgroundTaskManager

Source: `app/assistant/background_task_manager/`.

Thread-per-task daemon management. Default tasks:

| Task | Responsibility |
|------|----------------|
| `db_cleanup` | Periodic compaction / vacuum on `emi.db` |
| `watchdog` | Liveness checks for long-running daemons |
| `ticket_maintenance` | TicketManager state-machine sweeps (expire stale tickets, etc.) |
| `routine_runner` | Wakes `RoutineManager` on a clock |

Lifecycle:
- `start_all()` at bootstrap spawns one daemon per registered task.
- `stop_all()` at shutdown signals each daemon to exit.

## TicketManager

Source: `app/assistant/ticket_manager/`.

Type-agnostic CRUD with state machine.

- **States**: `pending` → `proposed` → `accepted` / `dismissed` /
  `snoozed` / `expired`.
- **Terminal states**: `completed`, `dismissed`, `expired`, `failed`.
- **Transition validation**: `_ALLOWED_TRANSITIONS` enforces legal
  edges. Illegal transitions raise rather than silently no-op.
- **Owners**: `create_dayflow_ticket` and similar tools are the
  primary writers; the dayflow orchestrator's `post_room_finalize_node`
  closes ticket-source items when a ticket is responded to.

Periodic maintenance (e.g., expiring stale `proposed` tickets) is run
by the `ticket_maintenance` daemon under `BackgroundTaskManager`.

## Feature-specific service managers

These are plain Python services, accessed via `DI`. None has a manager
YAML config; all are eagerly registered in `app/bootstrap.py`.

| Manager | Purpose |
|---------|---------|
| `AFKMonitor` | Active-first idle detection; records active segments, infers AFK from gaps. Used by RoutineManager's AFK guard. |
| `DJManager` | Music selection state machine with vibe planning and candidate selection. |
| `LocationManager` | User location tracking/prediction from calendar + patterns. Refreshed by the `location_refresh` routine. |
| `MaintenanceManager` | Daily summaries, db cleanup, log management, rate-limited events. |
| `PreferenceManager` | Feedback handling (thumbs up/down), delegates to `LabelAgent` for classification. |
| `UserSettingsManager` | User settings and feature flags storage/retrieval. Backed by a JSON file under `resources/`. |

## What service managers don't do

To make the contrast with [02_MANAGERS](02_MANAGERS.md) sharp:

| | Agent-orchestrating | Service |
|---|---|---|
| Runs an agent loop | Yes (`MultiAgentManager.request_handler`) | No |
| Has a per-invocation `Blackboard` | Yes | No |
| Configured via YAML in `multi_agents/` | Yes | No |
| Invoked through `ManagerInvoker.invoke()` | Yes | No |
| Returns a `ToolResult` per call | Yes | N/A — they're long-lived services, not invokable units |
| Lifecycle | Per-invocation (constructed, runs, returns) | Process-lifetime (started at bootstrap, stopped at shutdown) |
| Owns a thread | No (caller's thread runs the loop) | Sometimes (BTM owns its daemons; RoutineManager runs in BTM's `routine_runner`) |

If a "Manager" you're inspecting has any "Yes" in the agent-orchestrating
column, it belongs in [02_MANAGERS](02_MANAGERS.md). Otherwise it's
a service manager and belongs here.

## Key files

| File | Purpose |
|------|---------|
| `routine_manager/routine_manager.py` | Scheduled routine dispatcher |
| `routine_manager/routine_functions.py` | `ROUTINE_FUNCTION_REGISTRY` for `function`-type routines |
| `background_task_manager/background_task_manager.py` | Daemon thread management |
| `ticket_manager/ticket_manager.py` | Ticket CRUD state machine |
| `afk_monitor/afk_monitor.py` | Idle detection |
| `dj_manager/dj_manager.py` | Music selection |
| `location_manager/location_manager.py` | Location tracking |
| `maintenance_manager/maintenance_manager.py` | Periodic system maintenance |
| `preference_manager/preference_manager.py` | Feedback handling |
| `user_settings_manager/user_settings.py` | User flags / preferences |
