# Recipe: Add a new routine

A routine is a *schedule* for executing some piece of work. It tells the **RoutineManager** when to fire (a time policy or an event) and which runner type handles the execution.

Routines are **one JSON file per routine**, named `<id>.json`, in one of two folders:

- `configs/routines/public/` — tracked, shipped with the repo. For universal routines anyone running EmiOS would want.
- `configs/routines/private/` — gitignored, your personal extras (hardcoded camera ids, custom briefings, screen capture, ...).

Both folders are globbed and merged at load time; if the same `id` appears in both, the **private** file wins. `configs/routines.json` is now **settings only** (`schema_version`, `enabled`, `max_workers`, `state_resource_file`) — it no longer holds routine entries. `RoutineManager` reloads on every refresh tick (~60s), so editing a file takes effect with no restart.

> **Authoritative source:** `skills/extending-emi-routines/SKILL.md` — the entry shape, triggers, windows, and on_error live there. This recipe is the orientation; that skill is the spec. Framework details are in [06_PIPELINES_AND_ROUTINES.md](../architecture/06_PIPELINES_AND_ROUTINES.md).

Fastest start: copy the disabled example `configs/routines/public/example_camera_motion_poll.json` to `configs/routines/private/<your_thing>.json`, edit, flip `enabled: true`.

## The routine entry

```json
{
  "id": "my_routine",
  "enabled": true,
  "name": "Human-readable label",
  "aliases": ["my routine", "alt name"],
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

  "notes": "What it does, in one paragraph."
}
```

Required: `id`, `enabled`, `runner`, `spec`, and a schedule (`trigger` or, legacy, `run_policy`).

Quirk worth knowing: `run_policy: {}` is still required by the parser even when `trigger.policy` carries the real schedule. An entry with only `run_policy` and no `trigger` is treated as `{"type": "time", "policy": <run_policy>}` (this is why the existing `daily`/`pipeline` routines like `daily_insights_pipeline.json` still validate).

Optional but useful:
- `name` / `aliases` — display label + alternative names for chat-driven invocation ("run my routine now").
- `afk_guard` — skip when the user is away (or `require_afk` to run *only* while away). Fails closed: if AFK can't be determined, the routine skips.
- `feature_guard` — a user-feature-flag name (e.g. `"email"`); routine skipped unless `can_run_feature(name)` (feature enabled + API keys present).
- `manual_toggle` — the armed/disarmed-via-resource-file pattern (see the `screen_capture`/`activity_log` routines). Distinct from `enabled`.
- `notes` — free-form; shows in the `/routines` admin UI.

## Pick a runner type

Five options (`spec` key in parentheses):

| Runner | When to use | Spec |
|--------|-------------|------|
| `function` | The dominant runner. A Python function — the most flexible runner. | `spec.function_name` |
| `tool` | The work is a single registered tool call (e.g. `get_email`, `get_weather`). | `spec.tool_name` + `spec.arguments` |
| `pipeline` | A multi-step pipeline ([Add a pipeline](ADD_A_PIPELINE.md)). | `spec.pipeline_id` |
| `task` | A (compiled) task spec — declarative playbook run via a manager. | `spec.task_file` (+ `spec.compiled_file`) |
| `job` | Schedule-only legacy multi-task runner. Rare. | `spec.job_file` |

```json
// function runner — extra spec keys are read by the handler off routine.spec
"spec": { "function_name": "entity_card_refresh", "cooldown_days": 7, "max_rebuilds": 10 }

// tool runner
"spec": { "tool_name": "get_weather", "arguments": { "forecast_type": "current" } }

// pipeline runner
"spec": { "pipeline_id": "daily_insights" }

// task runner
"spec": { "task_file": "tasks/morning_briefing/task_spec.md",
          "compiled_file": "tasks/morning_briefing/morning_briefing.json" }
```

## `function` routines: register the handler

A `function` routine resolves `spec.function_name` against `ROUTINE_FUNCTION_REGISTRY` (`app/assistant/routine_manager/routine_functions.py`). Two ways to get there:

### Autodiscovery via `@routine_handler` (preferred)

Drop a file in `app/assistant/routine_handlers/<name>.py` and decorate the function:

```python
# app/assistant/routine_handlers/my_thing.py
from app.assistant.routine_handlers import routine_handler

@routine_handler()                       # or @routine_handler(name="custom_alias")
def my_handler(*, target_date=None, routine=None, event_message=None):
    # Read per-routine params off routine.spec; do the work.
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    ...
    # Raise on failure so on_error can back off. Return a dict/summary on success.
```

`discover_handlers()` (`routine_handlers/__init__.py`) walks the package at import time and folds every decorated function into the registry under its name (the decorator is explicit opt-in — undecorated helpers in the same module are NOT exposed). A hand-registered entry wins on a name collision (the duplicate auto-discovered one drops with a warning). See `routine_handlers/wiki.py` for a real example.

### Legacy: add to the registry dict

For handlers that still live in `routine_functions.py`, add the callable to `ROUTINE_FUNCTION_REGISTRY`:

```python
ROUTINE_FUNCTION_REGISTRY = {
    "my_handler": my_handler,
    ...
}
```

Either way the handler signature is keyword-only `(*, target_date=None, routine=None, event_message=None)`. Per-routine params come from `routine.spec` (e.g. `entity_card_refresh` reads `cooldown_days` / `max_rebuilds` / `max_new`). There is **no** `(spec_dict, run_ctx) -> RoutineRunResult` signature.

## Triggers: time vs. event

### Time (most common)

```json
"trigger": {
  "type": "time",
  "policy": { "type": "interval", "min_interval_seconds": 180 },
  "active_window": "sleep"
}
```

Policy variants (`run_policy.type`, evaluated each refresh tick by `_should_run`):

| Policy | Fires | Example |
|--------|-------|---------|
| `interval` | When `now - last_finished >= min_interval_seconds` | `{ "type": "interval", "min_interval_seconds": 300 }` |
| `daily` | Once per local calendar day at/after `time_local` | `{ "type": "daily", "time_local": "07:00" }` |
| `weekly` | Once per ISO week on `day_of_week` at/after `time_local` | `{ "type": "weekly", "day_of_week": "Monday", "time_local": "02:00" }` |

There is **no** `quiet_hours` policy type. Time-of-day restriction is `trigger.active_window`, not a policy. For `interval`, the refresh tick runs ~60s, so anything below 60s effectively becomes 60s.

### Event

```json
"trigger": { "type": "event", "topic": "ring_snapshot_captured" }
```

Fires on `event_hub` publish, not on the polling tick. `_wire_event_triggers` subscribes each event routine once per process; the handler receives the published message as the `event_message=` kwarg. Event routines are skipped by the time-polling loop. See `configs/routines/public/camera_dispatch.json`.

## Active windows

`trigger.active_window` is a named window from `configs/windows.json` (e.g. `sleep`, `work_hours`, `morning`, `evening`, `daytime`, `kg_active`):

```json
"active_window": "sleep"
```

Or inline:

```json
"active_window": { "from": "22:00", "to": "08:00", "local": true, "weekdays_only": false }
```

The window check runs **first** in `_should_run`, before any policy — outside the window the routine is skipped with reason `outside active window`. Windows wrap midnight automatically (`from > to`) and are re-resolved each tick, so a hot-edit of `configs/windows.json` takes effect without restart.

## On-error backoff and auto-disable

`on_error` default (applied at parse): `{ max_failures: 3, backoff_base_seconds: 60, backoff_max_seconds: 3600, then: "disable_with_ticket", auto_retry_after_seconds: 0 }`.

- Each failure increments `consecutive_failures` and pushes `next_attempt_after_utc` out by exponential backoff (`base * 2^(n-1)`, capped at `backoff_max_seconds`); `_should_run` blocks until then. Success resets the streak.
- On the `max_failures`-th consecutive failure, `then: "disable_with_ticket"` writes `enabled=false` to the status file and surfaces a `dayflow_notify` ticket. `then: "log_only"` keeps it enabled but still backs off.
- `auto_retry_after_seconds > 0` enables an auto-recovery probe: after that long since the auto-disable, the routine gets ONE attempt; success re-enables, failure pushes the next probe out.

## Watchdog (`max_run_seconds`)

A **soft** watchdog. Each refresh tick walks active threads; any run exceeding its `max_run_seconds` logs an error and surfaces a `dayflow_notify` ticket (once per run). Python can't kill threads from outside, so the run continues — but the in-flight guard prevents re-entry until it finishes (or the process restarts). `None`/`<=0` disables.

## Spec vs. status (the enabled toggle)

`configs/routines/<...>.json` is the **spec** (declarative: which routines exist, shipped defaults). `resources/resource_routine_status.json` is the **runtime state** written by the `/api/routines/<id>/toggle` UI and the auto-disable/recover machinery. The status file's `enabled` overrides the spec default per routine, so user toggles aren't clobbered by repo pulls and personal flags aren't pushed upstream. It also holds per-routine `last_*`, `run_count`, `consecutive_failures`, `next_attempt_after_utc`, `auto_disabled_reason/at_utc`.

## Scope (optional)

A routine may attach a sibling `<id>.scope.yaml` (permission-only; identity is stamped per run, never authored). `tool` / `function` payloads run under it; `pipeline` / `task` / `job` payloads self-scope. Most routines today ship no scope file. See [15_EMI_TEAM_AND_SCOPE.md](../architecture/15_EMI_TEAM_AND_SCOPE.md) and `docs/architecture/SCOPE.md`.

## Verify

After saving, the next refresh tick (within ~60s) loads it; watch logs for `ROUTINE STARTING` / `ROUTINE FINISHED`, and the decision log at `data/routine_decisions/<YYYY-MM-DD>.jsonl` (every fire / success / failure / interesting-skip). The `/routines` admin page shows the new entry immediately with its trigger / window / on_error / watchdog config.

Force a refresh tick instead of waiting:

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.routine_manager.routine_manager import get_routine_manager
get_routine_manager().refresh()
"
```

Or fire it now, bypassing scheduling:

```bash
.venv\Scripts\python.exe -c "
import app.assistant.tests.test_setup
from app.assistant.routine_manager.routine_manager import get_routine_manager
get_routine_manager().run_routine_now('my_routine')
"
```

Then check `resources/resource_routine_status.json` for your routine's `last_*` timestamps.

## Common pitfalls

- **Routine never fires.** Check `active_window` (outside it = skipped first, before any policy), `feature_guard` (disabled feature or missing API keys = silent skip), and `afk_guard` (fails closed when AFK is unknown). The `/routines` status card shows the last skip reason.
- **`run_policy` missing.** The parser still wants `run_policy: {}` even when `trigger.policy` holds the real schedule. Without it the entry may misparse.
- **Function runner errors with "function not found".** The `spec.function_name` didn't resolve. Either the `@routine_handler` file failed to import (check the startup warning) or you forgot to register it. The registry + autodiscovery are the only source of truth.
- **Routine auto-disabled itself.** Three consecutive failures with the default `on_error` flips `enabled=false` in the status file and files a ticket. Fix the handler, then re-enable via `/routines` (or set `auto_retry_after_seconds` for a self-healing probe).
- **`min_interval_seconds < 60`.** The refresh tick is ~60s; sub-minute intervals effectively become 60s and can double-fire on adjacent ticks. Use 60+ for true once-per-cycle semantics.

## See also

- `skills/extending-emi-routines/SKILL.md` — authoritative entry shape / triggers / windows / on_error
- [06_PIPELINES_AND_ROUTINES.md](../architecture/06_PIPELINES_AND_ROUTINES.md) — full framework reference
- [20_ROUTINES_ADMIN.md](../architecture/20_ROUTINES_ADMIN.md) — the `/routines` admin UI
- [Add a pipeline](ADD_A_PIPELINE.md) — for the `pipeline` runner case
