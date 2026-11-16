# Is it actually working: a daily health check (2026-11-15) — DESIGN, not built

Owner, 2026-11-15, on the assessment after a day of observation: "I am curious how you envision you can do 1 and 2";
then "write a design doc for #2". #2 was: an "is it actually working" check that finds a broken part the day it
breaks, not months later.

## Why

On 2026-11-15 several parts were wrong without anything raising an alarm, each found only by reading logs and prompts
by hand:

- `activity_tracker`: its form was refused by OpenAI on two calls (2026-08-01, 2026-11-15) and, separately, every
  successful call was shown no chat because the stage passed `recent_chat` while the prompt reads
  `recent_chat_excerpts`.
- The situation audit failed at 14:19 (`'dict object' has no attribute 'titles'`) while shared templates and Python
  disagreed.
- `subconscious::story_concern` needed a correction on 3 of 90 stories after a prompt change.
- The first chat of a session took 58 s, of which the model call was 1.6 s.
- `fetch_weather` failed on every run after 18:00 (the service refused TLS) after 14 successes earlier.
- Code that catches every error and returns a default (chat memory recall, the activity tracker stage, calendar
  fetch per calendar) hid failures from every reader.

Most of these are already in recorded data. Nothing reads that data for "is this part still doing its job".

## What it watches (all already recorded, except corrections)

| Source | What it gives | Where |
|---|---|---|
| `llm_call_log` | every model call: agent, time, status (`ok`, `error`, `parse_error`, `quota`, `rate_limit`, `timeout`), duration, tokens, cost | emi.db, since 2026-05-27 |
| routine decision log | each routine fired / succeeded / failed / auto-disabled, with duration and error | `data/routine_decisions/YYYY-MM-DD.jsonl` |
| `brain_runs` | each brain wake, its summary and `error` | emi.db |
| failure ledgers | items set aside after repeated failure (`kg_item_failure`, `belief_update_failures`) | emi.db |
| corrections (new) | each refused answer, whether its correction was accepted or raised `AgentOutputInvalid`, and the problems | new table, written by `agent_runtime/correction.py` |

The one addition to the runtime: `correction.run` records one row per refused answer (agent, time, outcome
`corrected` or `still_refused`, the problems' `why` text). Today this exists only as a log line.

A part's health is read from what it recorded doing, never from log text (owner, 2026-10-02: runs and steps are
recorded, not log-parsed).

## The daily snapshot (code only, no model call)

Once a day, and on demand, a routine computes for every agent and every routine, over the last 24 hours against the
7 days before:

- calls, successes, failures by status, corrections, corrections still refused;
- the time of the last success;
- median and 95th-percentile duration;
- input tokens and cost.

It is stored as one row per part per day, so a part's history is a query, and the page and tickets read the same
numbers.

## The rules: what turns a part red

Each part is judged against its own history, so no part needs configuring:

- **Quiet**: it ran on most of the last 7 days and has not run today, or runs far fewer times than usual. (A part that
  only runs on events is judged against the days it had those events, e.g. the activity tracker against days with
  chat.)
- **Failing**: its failure rate today is above its usual rate, or it has had no success for longer than its usual gap
  between successes while still being called.
- **Corrections rising**: its corrections today are above its usual rate, or any correction was still refused.
- **Slow**: its 95th-percentile duration is well above its usual.
- **Costly**: its daily cost is well above its usual (a prompt that doubled in size shows here first).

"Well above" is one rule for every part (for example, more than twice its 7-day median and above a small floor so a
part that usually takes 0.2 s is not red at 0.5 s). The rule and its floors are decided once, in one place.

A part is amber on the first day it trips a rule and red when it trips it two days running or when a rule trips hard
(e.g. no success at all in 24 hours while called). Each judgment carries its evidence in numbers.

## Where it shows

- **A `/health` page**: every part with its state, its numbers today against its usual, and for a failing part the
  calls or runs behind it (links to the call records; for brain agents, `/brain` already shows the exact prompts).
  Following the existing UI conventions (templates under app/templates, page CSS in app/static/css, the assistant's
  name from `{{ assistant_name }}`).
- **One ticket a day, only when something is red**, saying in words what is wrong with each red part, e.g.
  "subconscious::gate failed 3 of 41 calls today; it usually fails none." It respects the owner's quiet hours and
  is not repeated for a part already reported and still red; a part that recovers says so once.
- Nothing acts on its own: the check reports; fixing is the owner's call (owner standing rule: audit output is a
  hypothesis until the owner says go).

## The gap telemetry cannot see: swallowed errors

A failure caught and turned into a default before any call is made (recall returning `[]`, a stage logging a WARNING
and carrying on) leaves no failed call and no failed run. For these, a test walks the source with Python's `ast`
module (no regex) and lists every handler that catches all exceptions and returns, continues or passes without
re-raising. Existing ones go on an allow-list, each with its file and line and a note; a new one fails the test. The
list shrinks as each is fixed (the owner's rule "fail loud" is then enforced, not only stated).

## What it does not do

- It does not judge whether an answer was good: that is the prompt check (#1 of the assessment), separate.
- It does not retry, disable or repair anything.
- It does not read logs.
- It adds no model calls.

## Build order

1. Record corrections from `correction.run` (one table, one write).
2. The snapshot routine and its table.
3. The rules, with tests built from today's cases: the activity tracker's chat-less calls (quiet against days with
   chat), story_concern's corrections, the slow first chat, the failed situation audit, fetch_weather.
4. The `/health` page.
5. The daily ticket.
6. The swallowed-error test and its allow-list.

## Open questions for the owner

- The ticket: one a day only when red, or also a weekly "all green"?
- Which parts matter most if the page is long: everything ranked by state, or a short list of the parts the owner
  cares about first (chat, brain, dayflow, reminders)?
- Cost: show cost per part on the page, or keep the page about working/not working?
- Event-driven parts: is "judged against days that had its events" right, or should some parts declare what they
  depend on (e.g. the activity tracker depends on chat)?
