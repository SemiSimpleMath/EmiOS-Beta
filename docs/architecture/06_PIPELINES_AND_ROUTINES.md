# Pipelines and Routines

## Pipelines

Pipelines are sequential step-based execution frameworks for background data processing. Unlike agents (conversational), pipelines process data on a schedule or trigger.

### Architecture

**PipelineRunner** (`pipelines/step_runner.py`):
- Accepts a list of `PipelineStep` implementations
- Runs steps sequentially
- Tracks inputs, outputs, timing, errors
- Writes audit logs to `day_context/.../pipeline_runs/*.json`
- Idempotent: skips steps if outputs already exist (unless `force=True`)

**PipelineStep Protocol** (`pipelines/step_types.py`):
```python
class PipelineStep:
    name: str
    def inputs(ctx) -> List[str]
    def outputs(ctx) -> List[Path]
    def run(ctx) -> None
```

**PipelineContext** (`pipelines/context.py`):
- Date-aligned (boundary hour configurable)
- Provides: `pipeline_id`, `run_id`, `date_str`, `day_dir`, `snapshots_dir`, `pipeline_runs_dir`

### Active Pipelines

| Pipeline | ID | Schedule | Purpose |
|----------|----|----------|---------|
| Daily Insights | `daily_insights` | daily 00:05 | Archive assessments, tickets, timelines; apply insights |
| DayFlow | `dayflow` | interval 60s | Activity tracking, sleep analysis, routine scheduling |
| KG Pipeline | `kg_pipeline` | quiet hours | Ingest chat windows into knowledge graph |
| Entity Cards | `entity_cards` | daily 00:20 | Generate missing entity cards |
| Weekly Insights | `weekly_insights` | weekly Mon 00:10 | Cross-day pattern synthesis |
| Belief Engine (x6) | `belief_engine_*` | daily 00:30-01:00 | Domain-specific belief updates + export |
| KG Maintenance | `kg_maintenance_pipeline` | weekly Mon 02:00 | Orphan nodes, duplicates, suspect nodes |
| Entity Card Maintenance | `entity_card_maintenance_pipeline` | daily 02:00 | Broken links, junk names, stale content |

### Scope Policy

Each pipeline has a `scope.json` defining:
```json
{
  "resources": {
    "allowed_global_resources": [...],
    "denied_resources": [...]
  },
  "visibility": "...",
  "approval": "...",
  "execution": "..."
}
```

## Routines

Routines are the scheduling layer that executes pipelines, tools, tasks, and functions on configurable intervals.

### Configuration

`configs/routines.json`:

```json
{
  "schema_version": 3,
  "enabled": true,
  "max_workers": 12,
  "state_resource_file": "resource_routine_status.json",
  "routines": [
    {
      "id": "routine_id",
      "enabled": true,
      "name": "Display name",
      "aliases": ["alias1"],
      "runner": "tool",
      "spec": { "tool_name": "get_weather", "arguments": {} },
      "feature_guard": "weather",
      "run_policy": { "type": "interval", "min_interval_seconds": 660 },
      "afk_guard": { "skip_when_afk": true },
      "manual_toggle": { "resource_file": "resource_control.json" },
      "notes": "Description"
    }
  ]
}
```

### Runner Types (5)

| Runner | Config Key | Purpose |
|--------|-----------|---------|
| `tool` | `spec.tool_name` + `spec.arguments` | Invoke a registered tool directly |
| `task` | `spec.task_file` | Execute task spec via multi-agent manager or compiled task |
| `job` | `spec.job_file` | Orchestrate multiple tasks with dependencies |
| `function` | `spec.function_name` | Call a Python function from the function registry |
| `pipeline` | `spec.pipeline_id` | Execute a multi-step pipeline |

### Scheduling Policies (5)

**`interval`** — Run if `now - last_finished >= min_interval_seconds`
```json
{ "type": "interval", "min_interval_seconds": 300 }
```

**`daily`** — Run once per calendar day at a specific local time
```json
{ "type": "daily", "time_local": "07:00" }
```

**`weekly`** — Run once per ISO week on a specific day
```json
{ "type": "weekly", "day_of_week": "Monday", "time_local": "02:00" }
```

**`quiet_hours`** — Run once daily during configured quiet hour window
```json
{ "type": "quiet_hours", "feature": "kg" }
```

### Guards

**Feature Guard** — Skip if feature disabled or API keys missing:
```json
{ "feature_guard": "email" }
```
Calls `can_run_feature(feature_name)` from user settings.

**AFK Guard** — Skip if user is away:
```json
{ "afk_guard": { "skip_when_afk": true, "skip_when_potentially_afk": true } }
```

**Manual Toggle** — Skip unless explicitly armed:
```json
{ "manual_toggle": { "resource_file": "resource_control.json", "auto_off_time_local": "08:30" } }
```

### State Tracking

Persistent state in `resource_routine_status.json`:
```json
{
  "schema_version": 1,
  "routines": {
    "routine_id": {
      "last_run_utc": "...",
      "last_finished_utc": "...",
      "last_duration_s": 1.23,
      "last_status": "success",
      "last_error": "",
      "run_count": 42
    }
  }
}
```

State survives app restarts (unlike the old in-memory BackgroundTaskManager data fetch tracking).

### Capacity Management

```json
{
  "max_workers": 12,
  "capacity_warn_ratio": 0.80,
  "capacity_critical_ratio": 0.95,
  "capacity_alert_cooldown_seconds": 60
}
```

### Data Fetch Routines

Background data fetching (email, calendar, weather, news, tasks, scheduler) is managed as tool runner routines:

| Routine | Tool | Interval | Feature Guard |
|---------|------|----------|---------------|
| `fetch_email` | `get_email` | 300s (5m) | `email` |
| `fetch_calendar_events` | `get_calendar_events` | 1620s (27m) | `calendar` |
| `fetch_todo_tasks` | `get_todo_tasks` | 1860s (31m) | `tasks` |
| `fetch_weather` | `get_weather` | 660s (11m) | `weather` |
| `fetch_news` | `get_news` | 1380s (23m) | `news` |
| `fetch_scheduler_events` | `get_scheduler_events` | 180s (3m) | `scheduler` |

Tools compute their own defaults when arguments are missing (date ranges, locations, feed URLs).

### Execution Flow

`BackgroundTaskManager._run_routine_cycle()` calls `RoutineManager.refresh()` every 60 seconds:

1. Load config from `configs/routines.json`
2. For each enabled routine:
   - Check if already running (prevent duplicates)
   - Check worker capacity
   - Evaluate guards (manual toggle, AFK, feature)
   - Evaluate scheduling policy
   - If ready: launch in background thread
3. Thread executes: mark running -> dispatch to runner -> record result

## Key Files

| File | Purpose |
|------|---------|
| `routine_manager/routine_manager.py` | Scheduling, guards, state, execution |
| `routine_manager/runners/tool_runner.py` | Tool invocation runner |
| `routine_manager/runners/taskrunner/task_runner.py` | Task spec runner |
| `routine_manager/runners/pipeline_runner.py` | Pipeline runner |
| `routine_manager/runners/function_runner.py` | Function registry runner |
| `routine_manager/runners/job_runner.py` | Multi-task job runner |
| `routine_manager/run_types.py` | RoutineRunContext, RoutineRunResult |
| `pipelines/step_runner.py` | Sequential pipeline executor |
| `pipelines/context.py` | Pipeline context (date, paths) |
| `pipelines/scope_policy.py` | Pipeline scope enforcement |
| `configs/routines.json` | Routine definitions |
