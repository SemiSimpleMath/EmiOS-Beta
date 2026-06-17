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

- **5 runner types**: `task`, `job`, `tool`, `function`, `pipeline`
  (one runner per type under `routine_manager/runners/`).
- **3 scheduling policies** (`run_policy.type`): `daily`, `weekly`, and `interval`
  (the default). Active-window / AFK / manual / feature guards compose *on top* of
  the time policy — quiet-hours is one such window gate, **not** a policy type.
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

- **States**: `pending` → `proposed` → `accepted` / `snoozed` / `dismissed` /
  `expired`; an accepted ticket then runs `accepted` → `executing` → `completed` /
  `failed`.
- **Terminal states**: `completed`, `dismissed`, `expired`, `failed`.
- **Transition validation**: `_ALLOWED_TRANSITIONS` enforces legal edges. An illegal
  transition logs a `warning` and returns `False` — it does **not** raise (a raise is
  reserved for caller misuse, e.g. an unknown kwarg). Terminal states have an empty
  allowed-target set.
- **Owners**: `create_dayflow_ticket` and similar tools are the
  primary writers; the dayflow orchestrator's `post_room_finalize_node`
  closes ticket-source items when a ticket is responded to.

Periodic maintenance (e.g., expiring stale `proposed` tickets) is run
by the `ticket_maintenance` daemon under `BackgroundTaskManager`.

## Feature-specific service managers

These are plain Python services, accessed via `DI` or a module-level singleton.
None has a manager YAML config. **Lifecycle differs:** the first three plus
`UserSettingsManager` are eagerly registered in `app/bootstrap.py`; `PreferenceManager`
and `LocationManager` are **not** in the DI registry — they're constructed on demand
(per-request / lazy singleton) at their call sites.

| Manager | Purpose |
|---------|---------|
| `AFKMonitor` | Active-first idle detection; records active segments, infers AFK from gaps. Started + DI-registered at bootstrap. Used by RoutineManager's AFK guard. |
| `DJManager` | Music selection state machine with vibe planning and candidate selection. DI-registered (subsystem-gated). |
| `UserSettingsManager` | User settings and feature flags storage/retrieval. DI-registered as `user_settings`. Backed by a JSON file under `resources/`. |
| `PreferenceManager` | Feedback handling (thumbs up/down). Constructed per-request (e.g. `routes/idle_route.py`); delegates by building the `label` agent via `agent_factory.create_agent('label')` — `label` is an agent **name** (receiver string), not an imported class. |
| `LocationManager` | User location tracking/prediction from calendar + patterns. Module-level lazy singleton via `get_location_manager()`; refreshed by the `location_refresh` routine. |

## Other long-lived services

Same shape (DI-registered, no agent loop, no manager YAML), registered across the two
bootstrap phases (`app/bootstrap.py` Phase 1, `app/assistant/initialize_system.py`
Phase 2). See [17_SERVICE_LAYER](17_SERVICE_LAYER.md) for the full registry + wiring.

| Service (DI key) | Purpose |
|------------------|---------|
| `env_registry` (`EnvRegistryService`) | Env/secret + account registry; renders scope-filtered account views for `resource_accounts`. |
| `skill_registry` / `skill_injector` | `SKILL.md` loader (`skills/<name>/SKILL.md`) + per-skill `auto_inject_when` trigger evaluation. |
| `scheduler` (`SchedulerService`) | APScheduler `BackgroundScheduler`; auto-starts via `TimingEngine.__init__`. |
| `ticket_dispatcher` (`TicketDispatcherRegistry`) | Fans `proactive_suggestion` events out to every registered `TicketSurfaceAdapter` (socketio / telegram / slack / sms). |
| `mailbox` (`Mailbox`) / `mam_instance_manager` (`MAMInstanceManager`) | Manager-runtime mailbox + multi-agent-manager instance bookkeeping. |
| `outbound_chat_publisher` (`OutboundChatPublisher`) | Publishes assistant chat out to transports. |
| `chat_narrator` (`ChatNarrator`) | Narrates multi-agent activity into the chat surface; backs the display-name registry. |

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
| `afk_manager/afk_monitor.py` | Idle detection (`AFKMonitor`) |
| `dj_manager/manager.py` | Music selection (`DJManager`) |
| `location_manager/location_manager.py` | Location tracking (lazy singleton) |
| `preference_manager/preference_manager.py` | Feedback handling (per-request) |
| `user_settings_manager/user_settings.py` | User flags / preferences |
