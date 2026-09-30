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
  so it lands once. Chat: every user message in every room, verbatim, any length. Email (added the
  same day, step 1 of the brain build): every email the fetch keeps (email_parser importance >= 5),
  read from its pod as it is created — sender line, subject and full body, ref = the pod id,
  dated by its Date header (`ingest_email`, cursor = previous ingest time; pods minted by the
  one-time repository move are excluded). Only `role='user'` rows enter chat intake, so the brain's own
  output (digest, questions, replies) cannot come back as events.
- **Gate** (`app/assistant/subconscious/gate.py`, agent `subconscious::gate`). Routes each pending
  event against the open concerns: `concern` (naming every concern it bears on), `new_matter`, or
  `none`. A router, not the judge; when unsure it passes the event on. Labels are request-local
  (E1…, C1…) and mapped back by code. One correction round; a page still invalid after it is marked
  `failed`, and the noticer still reads those events. Anything passed on triggers a noticer tick
  (cooldown-guarded, shared with answer capture). Routine `brain_gate`, every 5 minutes; free when
  nothing new arrived.
- **Events are read inside their conversations** (`app/assistant/subconscious/conversations.py`,
  macro `agents/shared/macros/conversations.j2`; added the same day). Gate and noticer see events
  grouped by room, under the room's ROOM.md `description:` (who talks there, what it is), each inside
  its whole conversation: the room's user and assistant turns, split at 30 minutes of silence, walked
  back and forward to the conversation's ends, nothing cut. Local times, a date header per day.
  Events carry the reader's mark (gate `E1`…, noticer `[message:<id>]` plus where the gate routed
  it); other turns are context. This replaced the per-event `replying_to` (the latest assistant line
  in the room, of any age), which made every Slack message for three days "reply" to one old line.
  The `replying_to` column is no longer written; it stays in the schema.
  Email is read the same way: grouped by inbox under the account's `description` in
  configs/oauth_accounts.json (one factual line: whose inbox, what arrives there; required, raises
  when missing — `oauth_registry.get_description`), each email inside its Gmail thread (every stored
  email of the thread, in time order). The gate routes newsletters and promotions `none` unless
  their content bears on a concern.
- **Noticer reads reports** (until step 3, 2026-09-30, when the brain step took this over).
  Context item `brain_reports`: every unconsumed routed event, verbatim, inside its conversation,
  with its route. Prompt
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

## Step 2: the concern door (2026-09-30)

`app/assistant/subconscious/concern_door.py` is the only way a concern enters the register; the
noticer's `new_concerns` go through it, and the brain step, chat and dayflow will.

- Plan, outside the register lock: a candidate whose anchor the owner settled (declined, or
  accepted as chronic) is suppressed by code. The rest go to `subconscious::concern_door` beside
  every open concern, every concern resolved in the last 14 days and every dormant one. For each
  candidate: `new`, `same_open`, `same_closed` (settled, nothing since reopens it) or
  `same_candidate` (another candidate in the batch). Unsure means new: a duplicate costs a repeated
  notice, a false merge loses a matter. The next occurrence of a recurring need is new. Labels N#/C#
  are request-local. One correction round; still invalid raises and nothing is written (the
  noticer's reports stay unconsumed for its next tick).
- Apply, under the lock: code assigns the id (uuid4) and records `origin` (the source) and
  `created_at_utc`. A duplicate's evidence is appended to the concern it matches and the merge is
  journalled there (`MERGED a re-raise from <source>`); a repeat of a closed matter is journalled on
  it (`SUPPRESSED a re-mint`). The tick log records label -> concern_id.
- Every candidate states `done_when`, the outcome that closes it; the door refuses one without.
- The noticer's form: `concern_id` became `label` (N1…); questions and belief updates that refer to
  a just-raised concern by label are mapped to its real id.

Live check on the real register: a flea-medication candidate was judged the next monthly
occurrence (new), and two library-fine candidates were merged into the fine concern the noticer had
raised from the first email run.

Not in step 2, deliberately: enforcement that an owner-created concern closes only when its
`done_when` is met or the owner says so (owner-created concerns first exist with the brain step),
and the record of what was created for a concern (its writers are the dayflow handoff).

## Step 3: the brain step (2026-09-30)

`app/assistant/subconscious/brain_step.py`, agent `subconscious::brain`, run by the brain_gate routine
right after the gate. It replaces the noticer as the reader of what the gate passes on; the noticer no
longer has a reports section or `report_decisions` and reads the brain's work in the register
(journal lines `[brain] …`, `RESOLVED by the brain`, new concerns with `origin: brain`).

- Matters, grouped by code: events routed to the same concerns; new-matter or unrouted events of one
  conversation (chat room, or Gmail thread). A split that leaves two concerns about one thing is
  folded together by the concern door.
- Context per matter: the events inside their conversations with the gate's route on each; the
  room's chat summaries (chat_cluster pods) from 48 hours before the first event, and any since
  (owner request 2026-09-30: more than the current conversation, and the chat pod); the full record
  of every concern the events bear on, with evidence rendered as its text (the chat message, the
  pod) rather than its id, work outcomes and journal; the other open concerns, briefly.
- Answer: notes and resolutions on concerns, new concerns (done-when; the owner's words verbatim
  when the owner asked), and a decision with a reason on every event. Code checks labels, that each
  decision agrees with what cites the event, and that owner_words appear exactly in one of the
  concern's events; one correction round.
- Apply: new concerns through the concern door (source `brain`), notes and resolutions under the
  register lock (`persist.apply_brain_matter`; a note does not count toward disposition pressure),
  events marked consumed, the matter recorded in `brain_matters` (decisions, admitted ids). A matter
  still invalid, or one that fails to apply, is recorded `failed`: nothing written, its events stay
  in the inbox and are not retried automatically.
- Concerns the owner asked for carry `owner_request` {words, at, ref}. The noticer's resolutions and
  accept_chronic on them are refused and journalled; the concern door never judges one the same as a
  closed matter, and a merge carries the request onto the concern it joins.

Live dry run (2026-09-30, nothing applied): the 2026-09-28 "given the flea medication" message,
read as a new matter against today's register, resolved the flea concern the noticer had raised that
morning for the 2 October calendar entry; the school library-fine email became a note on the open
library-fine concern, with the pay-or-return details. Each without a gate route: the brain found the
concern from the list of open concerns.

## Step 4: association (2026-09-30)

`app/assistant/subconscious/work_links.py`; the brain step adds, per matter:

- Linked work, exact, by code: work whose `constraints.concern_refs` cites one of the matter's
  concerns (full id or the 8-character short form), and work whose `constraints.source_intake`
  came from an email in one of the matter's Gmail threads (account + thread_id).
- Similar past work, by meaning: `brain_work_index` (emi.db) holds one MiniLM vector per work object
  (title + objective), refreshed for new or changed objects only (first build: 1,513 objects in 44 s).
  Top 5 at cosine >= 0.36, best over every event text and concern title, linked work excluded.
  Calibration on the live store: true matches 0.74-0.84 (flea medication, OpenAI charges),
  0.53-0.58 (library fine), 0.38 ("haven't booked my physical" -> the active notify-to-schedule
  work); best unrelated 0.33-0.35; unrelated chat 0.22.
- Past work is shown as title, status, dates, why it ended, and each task's finalizer outcome
  (shared/work/finalizer_summary.j2, what the steward reads).
- Already in motion: every active work object, every scheduled reminder that can still fire
  (time_events), and the calendar for the next 30 days (read once per run, with calendar:<id>
  anchors). The prompt asks the brain to name, in its note, every item that depends on a fact
  that changed.

Live dry run (nothing applied): the 2026-09-28 flea message, now read with the calendar and active
work, became a note naming the 2 October calendar entry and the active 2 October dose work as
needing review because the dose was given four days earlier, instead of resolving the concern.
The library-fine email linked exactly to the work created from it (same Gmail thread) and found the
related "resolve the fine" work by similarity (0.63).

Not yet: KG-entity candidates (entity detection misses places; see the bug list), and search over
outbound messages that arranged something with someone. Linking what dayflow creates for a concern
at creation is step 6.

## Step 5: the concern brief (2026-09-30)

`app/assistant/subconscious/concern_brief.py`, agent `subconscious::brief`, run by the brain_gate
routine after the brain step. For every open concern whose record changed since its brief was written
(fingerprint `basis` over the record, the brief excluded), the writer reads the concern's full record
(evidence as text, journal, work outcomes), its linked and similar past work, and what is in motion
(active work, live reminders, 30 days of calendar), and writes: what, why it matters, known facts each
with a source, what was tried, the owner's wishes, what depends on it, open questions, a
recommendation. Every source must be a ref it was shown, or `journal` / `notes`; one correction round.
Stored on the concern as `brief` {…, basis, written_at} by `persist.set_concern_brief`, which refuses a
brief written from an older record. A failure is stored as `brief_error` with its basis and not retried
until the record changes.

Dayflow reads it: `work_context.work_data` carries `concerns` (the briefs of the concerns a work object
cites, `briefs_for_refs`), rendered by shared/work/concern_brief.j2 in the architect's portfolio and in
the worker and finalizer task context. A ref that resolves to no concern is logged and shown as
unresolved, not raised, because it renders on every pass. The planner (strategic_planner_wo) still
reads concerns from the daily-context snapshot; it gets briefs with the handoff (step 6).

Shared templates for both brain agents: agents/shared/brain/concern.j2, past_work.j2 (now with the
work id, so briefs can cite it), work_and_motion.j2.

Live dry run (not stored): the flea concern's brief named the 2 October calendar entry and the active
work as depending on it, and put what is unknown (medication on hand, dose given) in open questions;
the library-fine brief sourced every fact to the school's email and found in past work that two
earlier attempts to get the owner's choice expired unanswered.

## Knowledge-graph entities for the brain and the brief (2026-09-30)

`app/assistant/subconscious/kg_links.py`. The chat's card-based detector knows only carded entities
(Karjalohja has no card), and the context engine's convergence walk took over an hour a run (bug
list). Owner: search the entity nodes directly.

- `find_entities`: every Entity node (1,561) whose label or alias appears as whole words, the card
  detector's rules (possessive allowed), longest name first so "South Lake Middle School" does not
  also match "Lake". Multi-word names in any case; a single-word name only where the text
  capitalizes it, unless the text is all lowercase or the entity is central (PageRank >= 0.005: 39
  entities, the household's people, dogs and places). Without the case rule, a concern's long chat
  summaries matched Bed, Water, Dinner, Pizza, Ship. The primary user's nodes are left out.
- `shared`: in one set-based query, the Event/State nodes linked to two or more of the named entities
  (the graph joins entities through them) and the entities reached from two or more through one such
  node; 10 most important of each. The owner's brother + Karjalohja: the owner's wife and father, in 0.09 s.
- Both the brain (events' text) and the brief writer (title, notes, evidence text) get a "Named here"
  section: each entity's description, and what joins them.

Brief writer v2 (`WRITER_VERSION`, part of every brief's basis, so raising it rewrites every brief and
retries every failure): the first live run wrote 1 brief and failed 3 on source format, not
invention: the writer cited evidence as displayed ("pod datapod:…"), older bare ids, and two sources
at once. A source part is now valid when it contains a ref that was shown (or its id without the
`kind:` prefix), several may be separated by ";", and `journal`, `notes`, `knowledge graph` are
named sources. Scheduled reminders carry `reminder:<id>`: the writer had invented
"calendar:<a friend>'s birthday" for one. Live re-run of the three: all valid; the friend's-birthday brief cited the
reminder by its ref.

## Requirement for the brain build: association finds dependents (owner, 2026-09-30)

Finding what an event relates to is the most important part of the brain step. A change is rarely
local: "the bake sale moved to Monday" may require notifying the owner, moving reminders, changing
calendar entries, and emailing the friend who was going to drive. Association is impact analysis:
find the concern and every commitment built on the old fact — scheduler reminders, dayflow wakes and
held concerns, calendar events, todos, and outbound messages or emails that arranged something with
someone. The brain's handoff names each consequence; the architect plans the steps.

Two mechanisms, in this order:

1. Links set by code at creation. Anything dayflow creates for a concern (a reminder, a calendar
   event, an outbound message, a ticket) is recorded against that concern when it is created, so a
   later change follows the links.
2. Search for what was made outside a concern (the owner's own calendar entry, an arrangement made
   in chat): shared KG entities, dates, and similarity over sent messages, calendar and scheduler
   items. Code proposes candidates; the model decides which are the same matter.

First email run (2026-09-30, 44 emails over 72 hours): the gate routed 24 `new_matter`, 20 `none`,
0 failed. Four OpenAI funding receipts and four AWS support-case emails each became a separate new
matter. Grouping related events into one matter is part of the brain step's job.

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
