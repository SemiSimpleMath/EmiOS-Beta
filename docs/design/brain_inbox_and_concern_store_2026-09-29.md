# Brain inbox, gate and concern store (2026-09-29)

## Why

The noticer (the subconscious "brain") saw the household only through samples chosen by recipe:
the last 4 days of user messages between 60 and 220 characters, newest 12; chat clusters as
one-line titles; the last 10 dayflow items. On 2026-09-28 the owner wrote "OK I HAVE GIVEN [THE
DOGS] THEIR FLEA MEDICATION!" (55 characters). The length filter dropped it, and the flea
concern it settled stayed in every digest for two weeks. Nothing reported to the brain; it pulled
a sample and hoped.

Owner's direction, same day: most things should flow through the brain, with a pre-brain that
knows every open concern and lets things through; the brain says why something matters or does
not ("he does not care about LinkedIn"), and can give dayflow context it lacks.

## What was built

Step 1: information arrives.

- **Brain inbox** (`app/assistant/subconscious/brain_inbox.py`, table `brain_events` in emi.db).
  Each source writes one row per thing that happened, keyed by the source's own id (`message:<id>`)
  so it lands once. Chat is the first source: every user message in every room, verbatim, any
  length, with the assistant turn it replies to. Only `role='user'` rows enter, so the brain's own
  output (digest, questions, replies) cannot come back as events.
- **Gate** (`app/assistant/subconscious/gate.py`, agent `subconscious::gate`). Routes each pending
  event against the open concerns: `concern` (naming every concern it bears on), `new_matter`, or
  `none`. A router, not the judge; when unsure it passes the event on. Labels are request-local
  (E1…, C1…) and mapped back by code. One correction round; a page still invalid after it is marked
  `failed`, and the noticer still reads those events. Anything passed on triggers a noticer tick
  (cooldown-guarded, shared with answer capture). Routine `brain_gate`, every 5 minutes; free when
  nothing new arrived.
- **Noticer reads reports.** New context item `brain_reports`: every unconsumed routed event,
  verbatim, grouped under the concern it bears on, then new matters, then unrouted events. Prompt
  section A1a: a user's own words about a concern outrank calendar entries and earlier notes.
- **Every report gets a decision.** AgentForm `report_decisions`: `used` / `tracked_as_new` /
  `not_worth_tracking` with a reason, stored on the event (`noticer_decision`, `noticer_reason`).
  That is the "this matters because X / does not because Y" record. A report left without a decision
  stays unconsumed and is shown again.

Step 2: a database of concerns.

- **Concern store** (`app/assistant/subconscious/concern_store.py`, table `concerns` + meta). One
  row per concern: status (the bucket), position, the tracked state as columns (title, subject,
  kind, severity, horizon, anchor, first_observed, last_reinforced_utc, resolved/dormant/declined
  times) and the full record as JSON. The register dict stays the domain model, so persist.py's
  lifecycle logic is unchanged; only storage moved. Every reader (noticer context, digest,
  dashboard, dayflow projection, meal/wellness/romantic proposers, answer capture, gate) loads from
  the table. `brain_events.concern_ids` join to `concerns.concern_id`.
- **Migration** at app startup (`import_legacy_file_if_pending` in app/bootstrap.py): when the table
  is empty and `resources/subconscious/resource_concerns_register.json` exists, it is imported and
  renamed `*.imported-<date>.json`. A file beside a populated table is logged as an error and not
  read. Manual: `python -m app.assistant.subconscious.concern_store --import-legacy`.

## Verified

- Live gate run over 72 h (42 messages): both flea messages routed to the flea concern; the rat
  worry and a declined-card anomaly as new matters; the birthday request to its concern; chit-chat
  to none.
- Live noticer tick: read 14 reports, resolved the flea concern citing the owner's message.
- Second tick: all 7 new-matter reports got a decision and reason (all `not_worth_tracking`; the
  rat worry was judged a one-off, which is a prompt-calibration question for the owner).
- Tests: `test_brain_gate.py` (7), `test_concern_store.py` (9); the ten register test files moved
  to a scratch store (`app/assistant/tests/concern_store_helpers.py`); non-agent and dayflow suites
  show no new failures.

## Found on the way (fixed before shipping)

A first version imported the legacy file implicitly on the first read of an empty table. A test
running against a temporary test database read the empty table, imported the REAL register into
the test database, and renamed the real file. Nothing was lost (the file was renamed back), but it
is why the import is now explicit and only in app startup, which tests do not run against the real
data dir. A second slip: `ensure_schema(connect=_connect)` bound the default at definition time, so
a monkeypatched connection did not reach it and an empty `concerns` table was created in emi.db
from a test. All default connections now resolve at call time.

## Not done yet

- Other sources: email, calendar changes, ticket replies, dayflow outcomes, pods. Each needs an
  emitter into the inbox.
- The gate in front of dayflow: dayflow's intake triage still classifies chat and email on its own.
  The plan is one gate routing to both the brain and dayflow, with the brain's reasons attached as
  context.
- Time route: events for things entering their lead window (calendar, recurring obligations).
- Discovery over the full timeline (the length-filtered passing-mentions lane is still there).
- The digest is still clustered as chat by the pod classifier (the self-feed loop).
- Normalizing evidence and journal into their own tables (they live in the concern's JSON record).
- Deploy: needs an app restart (Python). The running process auto-disabled the new `brain_gate`
  routine (unknown function); its one-hour auto-retry probe re-enables it after the restart.
