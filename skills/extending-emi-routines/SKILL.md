---
name: extending-emi-routines
description: How to add a new routine to EmiOS. A routine fires on a time schedule, an event, or both — gated by active windows, AFK guards, and on_error backoff. Use when the task involves scheduling recurring or event-driven work.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "new routine"
      - "add routine"
      - "create routine"
      - "schedule a"
      - "cron"
      - "extend emi routines"
---

# Adding a new routine

Routines are declarative entries — one JSON file per routine, named
`<id>.json`, in one of two folders:

- **`configs/routines/public/`** — tracked, shipped with the repo. For
  universal routines anyone running EmiOS would want.
- **`configs/routines/private/`** — gitignored, your personal extras
  (hardcoded camera ids, custom morning briefings, screen capture, etc).

Both folders are merged into the active routine list at load time. If
the same `id` appears in both, the private file wins — useful for
customizing a tracked routine's cadence or active_window without
forking the public entry.

Top-level settings (`enabled`, `max_workers`, `state_resource_file`)
live in `configs/routines.json` and apply globally. The `RoutineManager`
reloads on every refresh tick — edit, save, no restart needed.

A canonical disabled example lives at
`configs/routines/public/example_camera_motion_poll.json`. Copy it to
`configs/routines/private/<your_camera>_motion_poll.json`, fill in your
camera id, flip enabled=true, done.

## Routine entry shape

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

  "afk_guard":  { "skip_when_afk": false },
  "on_error":   { "max_failures": 3, "backoff_base_seconds": 60,
                  "backoff_max_seconds": 3600,
                  "then": "disable_with_ticket" },
  "max_run_seconds": 300,

  "notes": "What it does, in one paragraph."
}
```

`run_policy: {}` is required for the legacy parser even when
`trigger.policy` carries the real schedule.

## Trigger types

**Time** (most common):
```json
"trigger": {
  "type": "time",
  "policy": { "type": "interval", "min_interval_seconds": 180 },
  "active_window": "sleep"        // or { "from": "22:00", "to": "08:00", "local": true }
}
```

Policy variants:
- `{"type":"interval","min_interval_seconds":N}` — every N seconds
- `{"type":"daily","time_local":"HH:MM"}` — once per day at local time
- `{"type":"weekly","day_of_week":"Monday","time_local":"HH:MM"}`

**Event** (fires on event_hub publish):
```json
"trigger": {
  "type": "event",
  "topic": "ring_snapshot_captured"
}
```

The handler receives the event message as `event_message=` kwarg.

## Runners

- `function` — calls a Python function. Function must be in
  `ROUTINE_FUNCTION_REGISTRY` (legacy, edit `routine_functions.py`)
  or auto-discovered via `@routine_handler()` in
  `app/assistant/routine_handlers/<name>.py` (preferred).
- `task` — runs a compiled task spec. `spec.task_file` points at
  the spec markdown, `spec.compiled_file` at the compiled IR.
- `pipeline` — runs a Step pipeline. `spec.pipeline_id` is the key.
- `tool` — calls a single tool. `spec.tool_name` + `spec.args`.
- `job` — schedule-only legacy runner.

## Auto-discovered function handler

Drop `app/assistant/routine_handlers/<name>.py`:

```python
from app.assistant.routine_handlers import routine_handler

@routine_handler()
def my_handler(*, target_date=None, routine=None, event_message=None):
    # Do the thing. Raise on failure (on_error handles backoff).
    ...
```

The decorator opt-in registers it as `my_handler`. Pass
`name="custom_alias"` to register under a different name.

## Active windows

Reference a named window from `configs/windows.json`:

```json
"active_window": "sleep"
```

Or inline:

```json
"active_window": { "from": "22:00", "to": "08:00", "local": true,
                   "weekdays_only": false }
```

The window check runs FIRST in `_should_run`, before any policy.
Outside the window: skipped with reason `outside active window`.

## On-error backoff and auto-disable

Default `on_error` is `{ max_failures: 3, backoff_base_seconds: 60,
backoff_max_seconds: 3600, then: "disable_with_ticket" }`. After
3 consecutive failures the routine writes `enabled=false` to the
status file and surfaces a ticket. `then: "log_only"` keeps it
enabled but still backs off exponentially.

## After saving the entry

The next refresh tick (within ~60s) loads it. Watch logs for
`ROUTINE STARTING` / `ROUTINE FINISHED`. The decision_log records
every fire / success / failure / skip-with-interesting-reason in
`data/routine_decisions/YYYY-MM-DD.jsonl`.

The `/routines` admin page shows the new entry immediately, with
its trigger / window / on_error / watchdog config visible.

## Canonical examples

- Time + active window: `kg_pipeline` (daily 23:00 in `kg_active`)
- Cadence + window: `sleep_camera_tick` (every 3 min in `sleep`)
- Event-triggered: `camera_dispatch` (on `ring_snapshot_captured`)
- Pipeline runner: `entity_cards_pipeline`

## Notes

- `manual_toggle` is for the armed/disarmed-via-resource-file
  pattern (see `screen_capture` routine). NOT a duplicate of
  `enabled` — different semantics.
- Routines with `runner: function` need their function discoverable
  before the routine runs. If `function_name` doesn't resolve, the
  routine errors and (after 3 fails) auto-disables.
- `max_run_seconds` is a SOFT watchdog. Python can't kill threads;
  this just alerts and surfaces a ticket if a routine runs too long.
- See also: `extending-emi-skills` if your routine handler needs a
  reusable instruction it can pull at runtime.
