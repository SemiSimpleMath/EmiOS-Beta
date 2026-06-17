# Routines Admin UI — `/routines`

## What it is

A web UI for inspecting and managing every scheduled routine. Renders four views (hour-by-hour timeline, interval grid, weekly grid, full table), supports inline edits (toggle enabled, change time / day / interval), and runs routines on demand. The **RoutineManager** re-reads config on every refresh tick (~60s), so edits apply at the next tick without a restart.

Routine specs live as **per-routine files** under `configs/routines/{public,private}/<id>.json` (`_load_config` globs both, private overriding public on id collision — mirroring `RoutineManager._load_config`). The monolithic `configs/routines.json` now holds only top-level settings (`enabled`, `max_workers`) plus a legacy `routines` array fallback for older installs. Writes follow a **spec/status split** (K8s-shape):
- **Policy edits** (`time_local`/`day_of_week`/`min_interval_seconds`) write back to the routine's own `_source_file` (or `configs/routines/private/<id>.json` for new entries).
- **`enabled` toggles** write to the status file `resources/resource_routine_status.json`, NOT the spec — toggles are per-machine runtime state, so users can pull upstream schema updates without losing their on/off flags and commit schema changes without pushing personal toggles into the public repo.

This is a thin admin page on top of the routine system documented in [06_PIPELINES_AND_ROUTINES.md](06_PIPELINES_AND_ROUTINES.md). Read that first if you want the underlying scheduler model.

## File layout

| File | Purpose |
|------|---------|
| `app/routes/routines_admin.py` | Flask blueprint — page route + API endpoints |
| `app/templates/routines_admin.html` | The page shell (no inline CSS, no inline JS) |
| `app/static/css/routines_admin.css` | All styles |
| `app/static/js/routines_admin.js` | View rendering + edit modal + polling |
| `configs/routines/{public,private}/<id>.json` | Per-routine spec files (the policy write-back target) |
| `configs/routines.json` | Top-level settings (`enabled`, `max_workers`) + legacy routines fallback |
| `resources/resource_routine_status.json` | Runtime status (`last_*`, `run_count`) + per-routine `enabled` overrides |

Blueprint registration: `app/create_app.py` (search for `routines_admin_bp`).

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/routines` | Render the page |
| `GET` | `/api/routines` | List all routines with config + state + computed `schedule_label` |
| `POST` | `/api/routines/<id>/toggle` | Toggle `enabled` (body: `{"enabled": bool}` optional — defaults to flip) |
| `POST` | `/api/routines/<id>/policy` | Patch `run_policy` fields (body: any subset of `time_local`, `day_of_week`, `min_interval_seconds`) |
| `POST` | `/api/routines/<id>/run-now` | Invoke `routine_manager.run_routine_now(id)` in a daemon thread |

## What `/api/routines` returns

```json
{
  "now_local": "2026-04-26T19:30:00",
  "now_utc":   "2026-04-27T02:30:00Z",
  "manager_enabled": true,
  "max_workers": 20,
  "routines": [
    {
      "id": "wiki_nightly_refresh",
      "name": "Wiki nightly refresh",
      "enabled": true,
      "runner": "function",
      "run_policy": {"type":"daily","time_local":"03:00","quiet_hours_ok":true},
      "policy_type": "daily",
      "schedule_label": "daily at 03:00",
      "notes": "...",
      "feature_guard": null,
      "afk_guard": null,
      "manual_toggle": null,
      "last_run_utc": "2026-04-26T10:00:12Z",
      "last_finished_utc": "2026-04-26T10:01:33Z",
      "last_status": "success",
      "last_error": null,
      "last_duration_s": 81.2,
      "run_count": 47
    },
    ...
  ]
}
```

Live state (`last_*`, `run_count`, and the per-routine `enabled` override) is read from `resources/resource_routine_status.json` (the file `RoutineManager` writes after each run). `enabled` is resolved as **status-override-wins-over-spec-default** in `_enrich_routine`.

`schedule_label` is derived from the trigger/policy shape:
- `trigger.type == "event"` → `"on event: <topic>"` — these have **no editable schedule** (the modal shows no time/day/interval input).
- `daily` → `"daily at HH:MM"`, `weekly` → `"<Day> HH:MM"`, `interval` → `"every <cadence>"`.

## The four tab views

### Hour-by-hour
24 rows, one per clock hour. Each daily routine plotted at its target time, weekly routines plotted at their target time with a `Day` prefix, quiet-hours routines pinned to row 00 with a dashed border. Current hour gets a subtle highlight.

Color coding: solid border + accent color = daily, orange border = weekly, dashed accent = quiet-hours, muted = disabled.

### Interval grid
Cards per continuous routine. Each card shows: name, ON/OFF pill, big cadence number (`every 5m`, `every 1h`), runner type, time since last finish, total run count.

### Weekly grid
7 columns, one per ISO weekday. Weekly routines grouped under their target day.

### All routines (table)
Full table with columns: Enabled / Name (+id) / Runner / Schedule label / Last run / Status pill / Run count / Actions (Edit, Run now, Disable/Enable). Sorted enabled-first then alphabetical.

## The edit modal

Click any item in any view → modal opens with:

- **Enabled** select (true/false)
- For **daily**: `time_local` `<input type="time">`
- For **weekly**: `day_of_week` select + `time_local` input
- For **interval**: `min_interval_seconds` numeric input (range 30..604800 enforced server-side)
- A **Run now** button
- Read-only metadata: id, runner, type, last run timestamp + relative, last status, last error, notes

Save flow:
1. If `enabled` changed, `POST /api/routines/<id>/toggle` first.
2. Build a `policy` patch from the policy-specific inputs.
3. If patch is non-empty, `POST /api/routines/<id>/policy`.
4. Reload the routine list, close the modal, toast.

## Validation (server-side)

`_validate_policy_patch` in `routines_admin.py` enforces:

- `time_local` matches `HH:MM` (00–23, 00–59); only valid for daily/weekly policies
- `day_of_week` is one of `Monday..Sunday`; only valid for weekly policies
- `min_interval_seconds` is an integer in `[30, 604800]` (30 seconds to 7 days); only valid for interval policies

Cross-type fields are explicitly rejected (`time_local on interval` → 400). This prevents the UI from putting a routine into a state the scheduler can't interpret.

## Atomic write-back

Both writers (`_save_config` for policy edits, `_save_state` for toggles) write via tmp-file + replace; policy edits hold the process-level `_CONFIG_LOCK`. `_save_config` writes **each routine to its own `_source_file`** (one file per routine), not a monolith — the synthetic `_source_file` key is stripped before serializing:

```python
for entry in config.get("routines") or []:
    target = entry.get("_source_file") or str(private_dir / f"{rid}.json")
    to_write = {k: v for k, v in entry.items() if k != "_source_file"}
    tmp = Path(target).with_suffix(...".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(to_write, f, indent=2, ensure_ascii=False)
        f.write("\n"); f.flush()
    tmp.replace(target)
```

Top-level settings in `configs/routines.json` (`enabled`, `max_workers`, `state_resource_file`) are **not** touched by this path.

Two encoding details that matter (both writers):
- **`encoding="utf-8"`** is explicit — without it Python defaults to `cp1252` on Windows, which silently mangles em-dashes and other multi-byte characters into mojibake. (See git history around 2026-04-26 for the recovery.)
- **`ensure_ascii=False`** keeps human-readable Unicode in the file at rest. Diffs after a UI edit stay focused on the actual change.

## Live status polling

The page polls `/api/routines` every 30 s. No WebSocket — straightforward fetch + re-render.

## Run-now flow

`POST /api/routines/<id>/run-now` calls `routine_manager.run_routine_now(id)` in a daemon thread and returns immediately. The UI toasts "Started <name>" — the user watches `last_run_utc` flip on the next poll.

`run_routine_now` bypasses the schedule check but still respects the running-set guard (refuses if the routine is already running). It executes `_execute_routine(propagate_exceptions=True)` so failures surface in the route log even though the response already returned.

## How routine edits propagate

```
UI edit → POST /api/routines/.../policy → _save_config writes configs/routines/<pub|priv>/<id>.json
         (toggle → /toggle → _save_state writes resources/resource_routine_status.json)
                                                |
                                                v
RoutineManager.refresh() (every ~60s) → _load_config() → re-globs the per-routine files fresh
                                                |
                                                v
                          Routine fires next according to new policy
```

There is no explicit reload signal — the scheduler is pull-based. Worst case: a 60-second lag before the new schedule takes effect. This is intentional (avoids races between the route handler and a tick that's mid-fire).

## Known limitations

- No "next-fire estimate" column. Computing it would require replicating `_should_run` logic in JS or adding a server-side helper. Open extension.
- No filter / search bar on the All Routines tab. Easy add — filter the in-memory `state.routines` array.
- No grouping by category/domain (KG vs wiki vs comms etc). Open follow-up: add a `category` field to routine config and a chip-row filter.
- No edit support for `feature_guard`, `afk_guard`, `manual_toggle`, `aliases`, `notes`. Intentional — these are infrequent edits and the JSON file is the right surface for them.

## Cookbook

**Add a category filter:**
1. Add `"category": "kg"` (or whatever) to routine entries in `routines.json`.
2. In `_enrich_routine`, surface `category` on the API response.
3. In `routines_admin.js`, add a chip row above the four tabs that filters `state.routines` by category.
4. Render-only change — no API or scheduler change needed.

**Surface next-fire estimate:**
Add a server-side helper in `routine_manager.py` like `def predict_next_fire(routine, now)` returning ISO datetime or None. Expose it on `_enrich_routine`. Render in the All Routines table and the edit modal.

**Bulk enable/disable a category:**
Add a `POST /api/routines/bulk_toggle` endpoint that takes a list of ids and an enabled flag, applies under the existing `_CONFIG_LOCK`. Trivial server-side; UI is a checkbox column + a header button.

## Cross-references

- [06_PIPELINES_AND_ROUTINES.md](06_PIPELINES_AND_ROUTINES.md) — the underlying scheduler, runner types, scheduling policies, guards
- [02_MANAGERS.md](02_MANAGERS.md) — RoutineManager itself
- The recipe [Add a routine](../recipes/ADD_A_ROUTINE.md) — picking runner + policy + guard
