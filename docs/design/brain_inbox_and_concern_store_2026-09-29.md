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

## Step 6: readiness and the handoff to dayflow (2026-09-30)

Before: concerns reached the steward (strategic_planner_wo) as prompt lines from the daily-context
snapshot, and work was linked to a concern only if the steward cited `concern:<id>` in `based_on`
(101 of 1,494 work objects did; the 2026-09-20 flea work did not).

- Readiness: the brief (writer v3) ends in `readiness`: act_now (a broad task stating the outcome,
  and why now), hold (a future local time, stored as `hold_until_utc`; when it passes the concern is
  briefed again and the decision remade), or no_action (handled, work in progress, the owner has it,
  or only to know). Validated by code; one correction.
- Handoff (`subconscious/concern_handoff.py`, brain_gate routine after the briefs): each open concern
  whose current brief says act_now becomes one dayflow intake item, `concern:<id>:<brief basis>`,
  written straight into the steward's inbox (state `artifact`, `evaluator_pending`, past triage),
  carrying the concern id, the task, why now and the brief rendered by shared/work/concern_brief.j2.
  Once per brief version; not while an earlier handoff item waits in the inbox or work citing the
  concern is active. A `concern_handoff` event pokes the dayflow scheduler.
- The steward must answer every inbox item (intake_review): cite it in `based_on` to make work, or
  review it no_action / defer with a reason. Work made from the item gets the concern in
  `constraints.concern_refs` from the item's record (`work_intake.concern_refs_of`, at creation and
  when attached to existing work), not from the steward's citation.
- The steward's answer is journalled on the concern (`HANDOFF <item>: the planner turned it into work
  / reviewed it as no_action / defer until …: reason`), post-commit; a failed journal write is
  logged. The brief writer is told: after a deferral or no action, act_now again only on something
  new.
- The steward's prompt no longer lists concerns from the daily snapshot (the side door): concerns
  reach dayflow only through handoff items. The snapshot still feeds chat_gate.

## Step 7: dayflow's own intake lanes retired (2026-09-30)

- Delegation: master_room's chat_gate decision `dayflow_delegate_tf` became `track_tf` /
  `track_description`: the router acknowledges the user and writes nothing. The brain reads the
  user's message itself (it is in the inbox verbatim) and keeps it as a concern with the owner's
  words; the delegation request row, its ingestion lane (`_load_dayflow_requests`,
  `_build_delegation_message`, `mark_dayflow_requests_ingested`) and `user_request` items are gone.
- Email: still ingested as dayflow items, because the state_mover wakes work waiting on a reply
  with new email and chat items (`work_wait_intake`). New email items are stored as wake context
  (`artifact`, `email_wake_context`, not evaluator-pending) and triage skips them as it skips chat,
  so dayflow never acts on an email directly; the brain does, through concern handoffs.
- Chat: unchanged; chat items were already wake context, never triaged.
- Context enricher: the two nodes, both agents and their tests are removed; triage_persist now
  routes straight to the steward. Its job (KG context on intake) is the brief's, and the remaining
  triaged intake is doorbell/bedroom camera pods.
- The steward's intake is now the brain's concern handoffs plus allowlisted pods.

## Seeing it work: the /brain page (2026-09-30)

`subconscious/brain_trace.py` records every model call of the brain's agents (gate, brain, concern
door, brief writer, noticer) and of the steward, at `LLMClient.call_structured_output`: the system
and user prompts exactly as sent, the structured result or the error, engine and duration, and the
tags of the block the call ran in (`trace(stage=…, event_ids=…, concern_id=…)`, set by the gate,
the brain step, the brief writer and the noticer run). Table `brain_calls`; a failed write is logged
and never fails the call. /brain gains three views over it: What the brain did (per matter: events,
calls, decisions), Concerns (brief, readiness, brief calls, handoffs with the steward's answer and
the steward call found by the item id in its prompt, linked work, journal), and Calls (all, by
agent). Handoff items are found by the concern id in their record (`concern_handoff._existing_items`),
which also finds the first two, named `concern:…` before the rename.

## Reading a ticket: /read/<ticket_id> (2026-09-30)

Owner: a long notification or question shows only its first lines in the popup, with a link to a page
where it reads in normal type with everything it rides on. The popup keeps the answer buttons; the
page only reads. `proactive_popup.messageOrLead`: a message over 400 characters shows its first two
lines (cut at a word near 240 characters) and "Read the whole ticket"; tool approvals are unchanged.

`ticket_manager/reading.py` assembles the page by code from existing links, no model call: the work
node in `trigger_context.work_node` (goal, the steward's reason, done-when, the step's directive,
source intake), or the noticer question in `trigger_context.question_id`; the concerns either
reaches (brief, done-when, the owner's words, earlier outcomes); the sources the work, the brief and
the concern evidence cite, as text (an email with the rest of its Gmail thread, a chat message, a
pod); knowledge-graph entities named (`kg_links`); other work on the same concerns (`work_links`).
Route `app/routes/ticket_reading.py` (local only), page `read_ticket.html/.css/.js`.

A question a dayflow worker asks through the generic `ask_user` tool carries the same `work_node`
and `dispatch_epoch`, taken from the execution owner of the attempt it runs in, so its page shows the
work too. A question asked from chat has no work and shows the message and entities.

## The brain runs when something happens (owner, 2026-09-30)

Owner: event driven, not the constant five-minute churn; chat read in finished chunks; "if I am quiet
in the chat for that long I am not interacting with it anymore". Replaces the `brain_gate` routine
(removed with its handler).

- `subconscious/brain_wake.py`: one thread runs `run_brain` (gate, brain step, briefs, handoffs) when
  the ingest service delivers a chat message or email (`handle_envelope`, a gut subscriber), when the
  concern register is saved from outside the brain (`concern_store.save_register` calls `poke`: work
  outcomes, the noticer, edits), when a waiting room goes quiet, and once at boot. Otherwise it
  sleeps. Its own register writes do not wake it. A failed run is logged and not retried.
- Chat is ready once the owner has been quiet in that room for `brain_inbox.QUIET` (5 minutes), all
  of the room's messages together; email is ready at once (`brain_inbox.ready`). The gate routes only
  ready events and reports when the next room goes quiet (`next_ready_at`); readiness is computed
  from the stored message times, so a restart loses nothing.
- Email enters the inbox at parser importance `MIN_EMAIL_IMPORTANCE` = 6 and up. Over 2026-09-27..30
  all 19 emails at 5 were newspaper newsletters or sales and the gate passed on none of them.
- Slack rooms (`EXCLUDED_ROOM_PREFIXES`) are left out of the brain for now: relaxed talk with
  friends, not something to act on.
- The ingest service polls every 2 minutes, so a message is read 5 to 7 minutes after the owner's
  last message in the room.

## Work attached to a concern (owner, 2026-09-30)

Owner: "the concern does not become a work object, it has a work object attached to it now so we can
see how it is progressing and what is being done about it"; "when work objects are worked on and
finalizer runs we update the concerns"; `addressing` means work is attached and being done,
`resolved` means completely done.

- The concern keeps `attached_work`: per work object its objective, status, attached time, every
  finalizer judgment (verdict, account, the owner's replies verbatim) and its ending.
- The work store writes a receipt in the transaction of each change (work_objects/concern_outbox.py):
  attached (the work cites the concern, at creation or revision), judged (every judgment of a main
  task, whatever the verdict), done/abandoned. Delivery (subconscious/concern_feedback.py) runs after
  creation, each judgment and each closure, and in evaluator prep.
- Attach: active → addressing. The last attached work ending: addressing → active (an explicit
  decline still parks it dormant). Only the brain resolves.
- Each judgment and ending is also a brain inbox event (source `work`), routed to its concerns by id
  without the gate, rendered under "Work <id>" in the brain's input; the brain notes what it
  established and resolves when the done-when is met or the owner says nothing more is needed.
  Work feedback no longer wakes the noticer.
- One-time startup move (persist.rederive_attached_work, owner-approved): every existing concern got
  `attached_work` from the work citing it and its old `work_outcomes` (dropped); buckets re-derived;
  concerns whose work had already ended got that ending as a brain event
  (concern_feedback.report_earlier_endings). Preview on a copy of the live register, 2026-09-30:
  65 concerns; work stress, fatigue/sleep, a son's college-event plan and the Claude API outage back to
  active with their endings sent to the brain; the library fine and the annual physical stay
  addressing; the Oct 2 flea medication moves to addressing (its work is running).
- The reading page and the noticer read `attached_work`.

Deploy hazard met on the way: deleting `configs/routines/public/brain_gate.json` before the restart
stopped the brain at once (routine configs hot-reload, the wake is Python): no brain run from
17:28 UTC until the restart. Remove a routine's config only in the same restart that starts its
replacement.

## Holds wake the brain; the steward judges whether another attempt is productive (owner, 2026-09-30)

- The event-driven wake had no clock: a brief holding until a time was re-briefed only when
  something else woke the brain. The wake now sleeps until the earlier of a quiet room and the
  earliest hold of a current brief (`concern_brief.next_hold_at`, `run_brain` → `next_wake_at`).
- Owner: concerns are for every consumer (the meal planner, the wellness proposer, dayflow); a
  hand-over asks dayflow whether it can take a concrete step; work ending does not settle the
  concern ("research therapists" done, the concern stays open); "the concern should carry history so
  the steward knows we just worked on this: is it productive to keep working on it?"
- The concern's attempts (`concern_brief.attempts`: every attached work, its judgments with the
  owner's replies, its ending) are rendered in the concern view the steward reads in a hand-over and
  every work agent reads (shared/work/concern_brief.j2); before, the steward had only the brief
  writer's one-line `tried`.
- Steward prompt, section 2a: work only for a concrete step dayflow can take now; another attempt
  only for a step not yet tried or something new since the last one; otherwise no_action with the
  reason (journalled on the concern). One step per work object, its criteria that step's own outcome;
  the brain decides when the concern is settled. (Work stress, 2026-09-30: the steward wrote the
  concern's whole question into the work and the owner was asked twice.)

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
