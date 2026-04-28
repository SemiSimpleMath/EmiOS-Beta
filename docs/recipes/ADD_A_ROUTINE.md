# Recipe: Add a new routine

A routine is a *schedule* for executing some piece of work. It tells the **RoutineManager** when to fire and what runner type handles the execution.

Routines live in `configs/routines.json`. You can edit the file directly OR use the `/routines` admin UI ([20_ROUTINES_ADMIN.md](../architecture/20_ROUTINES_ADMIN.md)) — both approaches write the same file.

Read [06_PIPELINES_AND_ROUTINES.md](../architecture/06_PIPELINES_AND_ROUTINES.md) for the framework.

## Pick a runner type

Five options:

| Runner | When to use |
|--------|-------------|
| `tool` | The work is a single tool call. E.g., `get_email`, `get_weather`. |
| `task` | The work is a task spec — declarative playbook with steps. Optionally pre-compiled to JSON. |
| `job` | A multi-task orchestration with dependencies. Less common. |
| `function` | A plain Python function registered in `routine_manager/routine_functions.py::ROUTINE_FUNCTION_REGISTRY`. |
| `pipeline` | A multi-step pipeline ([Add a pipeline](ADD_A_PIPELINE.md)). |

Most new routines are either `tool`, `function`, or `pipeline`. `task` is for compiled morning-routine-style flows.

## Pick a scheduling policy

Four options:

| Policy | When | Example |
|--------|------|---------|
| `interval` | Fire when N seconds elapsed since last finish | `{ "type": "interval", "min_interval_seconds": 300 }` |
| `daily` | Fire once per local calendar day at HH:MM | `{ "type": "daily", "time_local": "07:00" }` |
| `weekly` | Fire once per ISO week on a specific day at HH:MM | `{ "type": "weekly", "day_of_week": "Monday", "time_local": "02:00" }` |
| `quiet_hours` | Fire once per day during the configured quiet-hours window for a feature | `{ "type": "quiet_hours", "feature": "kg" }` |

For `daily` / `weekly`, "already succeeded today / this week" suppresses re-firing. For `interval`, the minimum is 30s in the admin UI; the underlying scheduler refresh tick is ~60s, so anything below 60s effectively becomes 60s.

## The routine entry

```json
{
  "id": "my_thing",
  "enabled": true,
  "name": "My Thing (display name)",
  "aliases": ["my thing", "the thing"],
  "runner": "pipeline",
  "spec": { "pipeline_id": "my_thing_pipeline" },
  "run_policy": { "type": "daily", "time_local": "03:00" },
  "feature_guard": "my_thing_feature",
  "afk_guard": { "skip_when_afk": true, "skip_when_potentially_afk": false },
  "manual_toggle": {
    "resource_file": "resource_my_thing_control.json",
    "auto_off_time_local": "08:30"
  },
  "notes": "Why this exists, why it runs at this time, what to expect."
}
```

Required: `id`, `enabled`, `runner`, `spec`, `run_policy`.

Optional but useful:
- `name` — display in admin UI; defaults to id.
- `aliases` — alternative names for chat-driven invocation ("run my thing now").
- `feature_guard` — a user-feature-flag name; routine skipped if disabled or required keys missing. Calls `can_run_feature(name)`.
- `afk_guard` — skip when user is away. `skip_when_afk` is the strict version; `skip_when_potentially_afk` is the looser version.
- `manual_toggle` — only fire when explicitly armed via a control resource file. Used for activity_log-style "ready when user arms it" routines.
- `notes` — free-form description. Show up in the admin UI.

## Spec shapes per runner type

```json
// tool runner
"spec": { "tool_name": "get_weather", "arguments": { "forecast_type": "current" } }

// task runner
"spec": {
  "task_file": "tasks/morning_briefing/task_spec.md",
  "compiled_file": "tasks/morning_briefing/morning_briefing.json",
  "execution_mode": "compiled_task"
}

// function runner
"spec": { "function_name": "wiki_nightly_refresh", "run_critic": true }

// pipeline runner
"spec": { "pipeline_id": "kg_pipeline" }

// job runner
"spec": { "job_file": "jobs/my_job.json" }
```

For function runners, the function MUST be registered in `app/assistant/routine_manager/routine_functions.py`'s `ROUTINE_FUNCTION_REGISTRY`. Add an entry like:

```python
ROUTINE_FUNCTION_REGISTRY = {
    "my_function_name": my_function_callable,
    ...
}
```

Where `my_function_callable` accepts `(spec_dict, run_ctx)` and returns a `RoutineRunResult`. Look at the existing entries for the signature.

## Adding via the admin UI

1. Add the new routine entry to `configs/routines.json` directly (the UI doesn't have a Create button today — that's an open follow-up).
2. Open `/routines` — your routine appears immediately (the page reads the file fresh on every load).
3. Click the routine to edit time, toggle enabled, or click Run now to fire immediately.

## Verify

```bash
# After saving, force a refresh tick — easier than waiting 60s
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.routine_manager import get_routine_manager
get_routine_manager().refresh()
"
```

Then check `resources/resource_routine_status.json` for your routine's last_run timestamp.

Or invoke directly bypassing scheduling:

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.routine_manager import get_routine_manager
get_routine_manager().run_routine_now('my_thing')
"
```

## Common pitfalls

- **Routine doesn't fire.** Check `feature_guard` — if the feature is disabled in user settings, the routine silently skips. Open `/routines` and look at the routine's status card.
- **Routine fires twice.** You set `min_interval_seconds: 30` — the refresh tick runs every 60s, so two ticks in a 30s window may both find it ready. Use `min_interval_seconds: 60+` for true once-per-cycle semantics.
- **Function runner errors with "function not found".** You forgot to add to `ROUTINE_FUNCTION_REGISTRY`. The registry is the only source of truth; the function name in `spec` must match a key.
- **Daily routine runs in the middle of the night UTC.** `time_local` is local time per the host machine. Confirm `now_local` on `/routines` matches your wall clock.
- **Manual_toggle routine never fires.** The `resource_file` referenced must contain a JSON dict with `armed: true` for the routine to consider firing. If `auto_off_time_local` is set, it auto-disarms past that time.

## See also

- [06_PIPELINES_AND_ROUTINES.md](../architecture/06_PIPELINES_AND_ROUTINES.md) — runner types, scheduling policies, guards
- [20_ROUTINES_ADMIN.md](../architecture/20_ROUTINES_ADMIN.md) — the `/routines` admin UI
- [Add a pipeline](ADD_A_PIPELINE.md) — for the pipeline runner case
