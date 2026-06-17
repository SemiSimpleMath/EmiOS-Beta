# Pipelines and Routines

## Pipelines

Pipelines are sequential step-based execution frameworks for background data processing. Unlike agents (conversational), pipelines process data on a schedule or trigger.

### Architecture

**PipelineRunner** (`pipelines/step_runner.py`):
- Accepts a list of `PipelineStep` implementations; runs them sequentially.
- Idempotent: skips a step when all its `outputs(ctx)` already exist (`outputs_exist`), unless `force=True`.
- Stops on the first step that raises (`overall_status="error"`, `break`); a partial run is still recorded.
- Always writes an audit JSON to `ctx.audit_path()` (`<day_dir>/pipeline_runs/<run_id>.json`), then prunes the dir to the most recent 1500 files.
- `only_steps=[...]` restricts the run to named steps.

**PipelineStep Protocol** (`pipelines/step_types.py`):
```python
class PipelineStep:
    name: str
    def inputs(ctx) -> List[str]
    def outputs(ctx) -> List[Path]
    def run(ctx) -> None
```

**PipelineContext** (`pipelines/context.py`):
- Date-aligned to the local day-boundary hour (`boundary_hour_local`, default 5, read from `configs/dayflow_pipeline.json`).
- Built via `PipelineContext.for_date(pipeline_id=..., target_date=..., run_id=...)`.
- Provides: `pipeline_id`, `run_id`, `date_str`, `day_dir` (`day_context/<YYYY>/<MM>/<date>`), `snapshots_dir`, `pipeline_runs_dir`, plus `start_local/end_local/since_utc/until_utc`.

### Registered Pipelines

The pipeline registry (`pipelines/pipeline_registry.py`) lazily registers exactly these via `resolve_pipeline(id)`; a missing optional dependency is logged and skipped, not fatal:

| Pipeline ID | Factory | Purpose |
|-------------|---------|---------|
| `daily_insights` | `daily_insights.pipeline.DailyInsightsPipeline` | Archive context + tickets, build timeline, extract insights, apply, build assessment + summary |
| `dayflow` | `dayflow.pipeline.DayFlowPipeline` | Step-based DayFlow activity/sleep/routine pipeline |
| `kg_pipeline` | `kg_pipeline.pipeline.KGPipeline` | Ingest chat windows into the knowledge graph (bucket-per-stage) |
| `weekly_insights` | `weekly_insights.pipeline.WeeklyInsightsPipeline` | Cross-day pattern + belief-candidate synthesis |
| `belief_engine` | `belief_engine.pipeline.routine_adapter.BeliefEngineAdapter` | **One** pipeline that loops every domain marked enabled in `configs/belief_domains.yaml`, then exports beliefs inline |
| `belief_engine_export` | `belief_engine.pipeline.routine_adapter.BeliefEngineExportAdapter` | Manual re-export only (belief_engine exports inline at end of its run) |
| `kg_maintenance_pipeline` | `kg_maintenance_pipeline.pipeline.KGMaintenancePipeline` | Orphan nodes, missing descriptions, suspect nodes, duplicate pairs |

There is **no** `entity_cards` pipeline and **no** `entity_card_maintenance_pipeline`. Entity card upkeep runs as the `entity_card_refresh` **function** routine (`pipelines/entity_cards_v2/refresh_subscriber.run_card_refresh`). `belief_engine` is a single domain-looping pipeline — not six per-domain registrations.

### Scope Policy

A pipeline OPTIONALLY declares its permissions in a `scope.yaml` (permission-only). Identity
(`owner_id`, `actor_id`, `surface`) is stamped per run by the caller via
`load_scope_for_source(kind=..., source_id="<id>")` — never authored in the file:
```yaml
approval:
  authority_level: 0
tools:
  allowed_tools: []            # fail-closed; list tools only if the pipeline calls them
resources:
  allowed_global_resources: [all]
  resource_groups: [chat, memory]
pods:
  allowed_scopes: [self]
writes:
  write_unified_log: true
  write_kg: false
  allow_fact_extraction: false
```

## Routines

Routines are the scheduling layer that executes pipelines, tools, tasks, jobs, and functions on triggers (time or event), gated by active windows, AFK/feature/manual guards, and on-error backoff.

### Configuration model

`configs/routines.json` holds **settings only** — it no longer carries routine entries:

```json
{
  "schema_version": 3,
  "enabled": true,
  "max_workers": 20,
  "state_resource_file": "resource_routine_status.json"
}
```

Routine entries are **one JSON file per routine**, named `<id>.json`, in two folders (`RoutineManager._load_config`):

- `configs/routines/public/*.json` — tracked, shipped with the repo (universal routines).
- `configs/routines/private/*.json` — gitignored, personal extras.

Both folders are globbed and merged at load time; on an `id` collision the **private** file wins (`<name>.compiled.json` siblings are skipped — they're compiled task IR, not routine configs). An inline `"routines": [...]` array on `configs/routines.json` is still read as a **legacy fallback** (its entries fold in if not already seen) but new installs don't use it. `RoutineManager` reloads on every refresh tick — edit a file, save, no restart.

Authoritative how-to for the entry shape, triggers, windows, and on_error: `skills/extending-emi-routines/SKILL.md`.

### Routine entry shape

```json
{
  "id": "my_routine",
  "enabled": true,
  "name": "Human-readable label",
  "aliases": ["my routine"],
  "runner": "function",
  "spec": { "function_name": "my_handler" },
  "trigger": {
    "type": "time",
    "policy": { "type": "interval", "min_interval_seconds": 600 },
    "active_window": "daytime"
  },
  "run_policy": {},
  "afk_guard": { "skip_when_afk": false },
  "on_error": { "max_failures": 3, "backoff_base_seconds": 60,
                "backoff_max_seconds": 3600, "then": "disable_with_ticket" },
  "max_run_seconds": 300,
  "notes": "What it does."
}
```

### Runner Types (5)

| Runner | Config Key | Purpose |
|--------|-----------|---------|
| `function` | `spec.function_name` | Call a Python function from the function registry — **now the dominant runner** |
| `tool` | `spec.tool_name` + `spec.arguments` | Invoke a registered tool directly |
| `task` | `spec.task_file` (+ `spec.compiled_file`) | Execute a (compiled) task spec via multi-agent manager |
| `pipeline` | `spec.pipeline_id` | Execute a registered multi-step pipeline |
| `job` | `spec.job_file` | Schedule-only legacy multi-task runner |

`RoutineManager` constructs one runner instance per type; `FunctionRoutineRunner` is given the `ROUTINE_FUNCTION_REGISTRY`.

### The function registry + autodiscovery

`function` routines resolve `spec.function_name` against `ROUTINE_FUNCTION_REGISTRY` (`routine_manager/routine_functions.py`). Two ways to register:

1. **Legacy** — add an entry to the registry dict in `routine_functions.py`.
2. **Autodiscovery (preferred)** — drop `app/assistant/routine_handlers/<name>.py` and decorate a function with `@routine_handler()` (`routine_handlers/__init__.py`). `discover_handlers()` walks the package at import time and folds every decorated function into the registry under its name (or `name="alias"`). A hand-registered entry wins on a name collision (the auto-discovered duplicate drops with a warning). Handlers receive `target_date=`, `routine=`, and — for event routines — `event_message=` kwargs; they should raise on failure so `on_error` can back off.

### Triggers

Each routine carries a `trigger` (`RoutineConfig.trigger`). An entry with only the legacy `run_policy` and no `trigger` is treated as `{"type": "time", "policy": <run_policy>}`. Supported types (validated at load — an unknown type or window name fails loud):

- **`{"type": "time", "policy": <run_policy>, "active_window": <name|inline>}`** — evaluated each refresh tick by `_should_run`. If `trigger.policy` carries the cadence but `run_policy` is empty, the policy is lifted up so the scheduler sees it (otherwise it would fire every tick).
- **`{"type": "event", "topic": "<event_hub topic>"}`** — fires on `event_hub` publish, not on the polling tick. `_wire_event_triggers` subscribes each event routine exactly once per process; the handler re-reads `enabled` at fire time and honors the same in-flight + AFK guards as time routines. Example: `camera_dispatch` fires on `ring_snapshot_captured` (published by a camera motion-poll routine). Event routines are skipped by the time-polling loop.

### Scheduling policies (3, time triggers only)

`_should_run` reads `run_policy.type` (default `interval`). Only these exist:

**`interval`** — run if `now - last_finished >= min_interval_seconds`
```json
{ "type": "interval", "min_interval_seconds": 300 }
```

**`daily`** — run once per local calendar day at/after a local time
```json
{ "type": "daily", "time_local": "07:00" }
```

**`weekly`** — run once per ISO week on a specific day at/after a local time
```json
{ "type": "weekly", "day_of_week": "Monday", "time_local": "02:00" }
```

There is **no** `quiet_hours` policy type. "Quiet hours" / time-of-day restriction is expressed with `trigger.active_window` (see below). (Some entries carry a cosmetic `quiet_hours_ok` flag in `run_policy`; it is not read by `_should_run`.)

### Active windows

`trigger.active_window` is a named window from `configs/windows.json` (e.g. `sleep`, `work_hours`, `morning`, `evening`, `daytime`, `kg_active`) or an inline `{"from": "22:00", "to": "08:00", "local": true, "weekdays_only": false}` (`routine_manager/windows.py`). The window check runs **first** in `_should_run`, before any policy — outside the window the routine is skipped with reason `outside active window`. Windows wrap midnight automatically (`from > to`). Windows are re-resolved each tick so a hot-edit of `configs/windows.json` takes effect without restart.

### Guards

**Feature guard** — `"feature_guard": "email"`: skip unless `can_run_feature(name)` (feature enabled + API keys present).

**AFK guard** — `"afk_guard": { "skip_when_afk": true, "skip_when_potentially_afk": true }`. Also supports `require_afk` / `require_potentially_afk` (routines that should run only while away). **Fails closed**: if AFK status can't be determined the routine is skipped.

**Manual toggle** — `"manual_toggle": { "resource_file": "...", "auto_off_time_local": "08:30" }`: runs only while the resource file says `enabled=true`; auto-disables (once per local day) at the configured time. Never auto-enables. Distinct from `enabled` (used by armed/disarmed routines like screen capture).

### On-error backoff and auto-disable

`on_error` defaults (applied at parse): `{ max_failures: 3, backoff_base_seconds: 60, backoff_max_seconds: 3600, then: "disable_with_ticket", auto_retry_after_seconds: 0 }`.

- Each failure increments `consecutive_failures` and sets `next_attempt_after_utc` to an exponential backoff (`base * 2^(n-1)`, capped at `backoff_max_seconds`); `_should_run` blocks until that moment passes. Success resets the streak.
- On the `max_failures`-th consecutive failure: `then="disable_with_ticket"` writes `enabled=false` to the status file and surfaces a `dayflow_notify` ticket. `then="log_only"` keeps it enabled but still backs off.
- `auto_retry_after_seconds > 0` enables an auto-recovery probe: after that long since the auto-disable, the routine gets ONE attempt; success clears the disable (re-enables), failure pushes the next probe out.

### Watchdog (`max_run_seconds`)

A **soft** watchdog. Each refresh tick walks active threads (`_check_watchdogs`); any run exceeding its `max_run_seconds` logs an error and surfaces a `dayflow_notify` ticket (once per run_id). Python can't kill threads from outside, so the run continues — but the in-flight guard prevents re-entry until it finishes (or the process restarts). `None`/`<=0` disables.

### Spec vs status (the enabled toggle)

`configs/routines/<...>.json` is the **spec** (declarative: which routines exist, how they're wired, shipped defaults). `resource_routine_status.json` is the **runtime state** written by the `/api/routines/<id>/toggle` UI and by the auto-disable/recover machinery. The status file's `enabled` overrides the spec default per routine (`_read_state_enabled_map`), so user toggles aren't clobbered by repo pulls and personal flags aren't pushed upstream. The status file also holds per-routine `last_*`, `run_count`, `consecutive_failures`, `next_attempt_after_utc`, `auto_disabled_reason/at_utc`.

### Data fetch routines

Background data fetching is implemented as `tool`-runner routines (each with a `feature_guard`); each tool computes its own defaults when arguments are missing (date ranges, locations, feed URLs):

| Routine | Tool | Interval | Feature guard |
|---------|------|----------|---------------|
| `fetch_email` | `get_email` | 300s (5m) | `email` |
| `fetch_calendar_events` | `get_calendar_events` | 1620s (27m) | `calendar` |
| `fetch_todo_tasks` | `get_todo_tasks` | 1860s (31m) | `tasks` |
| `fetch_weather` | `get_weather` | 660s (11m) | `weather` |
| `fetch_news` | `get_news` | 1380s (23m) | `news` |
| `fetch_scheduler_events` | `get_scheduler_events` | 180s (3m) | `scheduler` |

### Execution flow

`BackgroundTaskManager._run_routine_cycle()` calls `RoutineManager.refresh()` every ~60s (gated by `is_subsystem_enabled("routine_manager")`):

1. Load settings + glob the public/private routine files.
2. Wire any new event-triggered routines to `event_hub` (once per id per process).
3. Run watchdog checks; emit capacity alerts (`max_workers`, default 20).
4. For each time-triggered routine: skip if disabled (unless an auto-recovery probe is due), already running, or at worker capacity; then evaluate guards + window + policy via `_should_run`.
5. If ready: launch in a monitored background thread → mark running → dispatch to the runner → record result + backoff/auto-disable to the status file.

Routines OPTIONALLY attach a scope (sibling `<id>.scope.yaml`); `tool`/`function` payloads run under it, while `pipeline`/`task`/`job` payloads self-scope. The decision log records every fire/success/failure/interesting-skip under `data/routine_decisions/<YYYY-MM-DD>.jsonl`.

## Key Files

| File | Purpose |
|------|---------|
| `routine_manager/routine_manager.py` | Trigger eval, guards, windows, on_error, watchdog, state, execution |
| `routine_manager/windows.py` | Active-window resolution (`configs/windows.json` + inline) |
| `routine_manager/routine_functions.py` | `ROUTINE_FUNCTION_REGISTRY` + handler autodiscovery wiring |
| `routine_handlers/__init__.py` | `@routine_handler` decorator + `discover_handlers()` |
| `routine_manager/runners/{tool,task,pipeline,function,job}_runner.py` | Per-runner dispatch |
| `routine_manager/run_types.py` | `RoutineRunContext`, `RoutineRunResult` |
| `pipelines/pipeline_registry.py` | Lazy pipeline registry (`resolve_pipeline`) |
| `pipelines/step_runner.py` | Sequential idempotent pipeline executor |
| `pipelines/context.py` | `PipelineContext` (date boundary, paths) |
| `pipelines/step_types.py` | `PipelineStep` protocol, `StepResult`, `outputs_exist` |
| `scope/loader.py` | Routine / pipeline scope loading (`load_scope_for_source`) |
| `configs/routines.json` | Routine manager **settings** (no entries) |
| `configs/routines/{public,private}/*.json` | One file per routine |
| `configs/windows.json` | Named active windows |
| `skills/extending-emi-routines/SKILL.md` | Authoritative how-to for authoring a routine |
