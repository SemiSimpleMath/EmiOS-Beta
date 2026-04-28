# Dayflow Resource Contracts

## `resource_expected_calendar.json`

Canonical machine-readable schedule for the current boundary day.

- Required keys: `date`, `expected_schedule`, `source`, `last_updated`, `last_updated_utc`
- `expected_schedule` must be a non-empty list of objects.
- Each schedule object should include:
  - `title`, `start_local`, `end_local`, `status`, `source`, `calendar_item_id`
  - `start_utc`, `end_utc` as UTC ISO timestamps when known
- Time normalization is strict; malformed local clock strings fail loudly.

## `resource_expected_calendar_markdown.md`

LLM-ready rendering of `resource_expected_calendar.expected_schedule`.

- Human-readable, concise list format
- Mirrors the same schedule semantics as JSON
- Intended for prompt clarity, not for internal time math

## `resource_daily_context_generator_output.json`

Daily context envelope used by downstream dayflow readers.

- Contains non-schedule context (`day_theme`, `milestones`, `current_status`, `calendar_events_structured`)
- Also contains `expected_schedule`, but schedule consumers should treat
  `resource_expected_calendar.json` as the source of truth.
