# The day's record: what the daily context tracker is for, and how it should get there (design, 2026-10-09)

Status: DESIGN for the owner's review. No code yet. Builds on docs/design/objects_and_projections_2026-10-08.md.

## 1. What it is for (owner, 2026-10-09)

"It starts with nothing but blank google calendar at the start of the day. It builds the day calendar then over the
course of the day from tickets, what user said etc. users health everything. Its job is to build the context for the
day in a calendar form and also keep track of major milestones that happened today. This is one of the main inputs to
daily insights which then become beliefs."

So the tracker keeps the day's record: what the day actually held, in calendar form, and what happened. At the day's
end the record is archived (daily_insights/archive_daily_context: `daily_context.md`, each event as "title time
(status)", the theme, the milestones) and daily insights reads it as what happened; beliefs form from it. Whatever
the record says becomes a fact about the owner's life.

During the day the same record is the "today's schedule" every dayflow agent reads (architect, state mover, action
selector, intake triage, cron tickets) and the chat gate, situation auditor, health inference and others read
(resource_expected_calendar, resource_daily_context_generator_output).

## 2. How it works today (read 2026-10-09)

- `DailyContextGeneratorStep` (pipelines/dayflow/steps/daily_context_generator_stage.py) runs every few minutes
  (hourly while the owner is away). At the day boundary it starts from the Google calendar alone.
- Each run calls `daily_context_tracker` with: presence; the user; the "previous daily context" (its own stored
  output, re-sent whole as JSON); the expected calendar (as a list with ids, and again as markdown); upcoming and
  completed Google events; recent chat (1 hour); ticket replies; activity segments; desktop activity; the entity card.
- The agent re-emits the whole schedule (every event with `calendar_item_id` copied, `source`, `updated_at`), the day
  theme, the milestones (time, description, evidence Reported/Calendar/Inferred) and a current status.
- The step writes that output, adds the Google events again (`calendar_events_structured`) and merges the brain's
  open concerns with their whole journals and the week's arbiter plan into the same resource (for the chat gate and
  the situation auditor). The next run reads all of it back as "previous daily context".

What a run on 2026-10-09 08:12 showed:
- Most of the prompt was not the tracker's: concern journals with ids and a raw reply JSON, the library concern's
  eight identical "keep_active" lines, meal plans with pod ids. The schedule appeared five times in three formats.
- The answer was a copy of its input with one change; every event's id copied by hand.
- What it decided was wrong: "Take out the dogs (8:00-8:30): completed" at 8:12, while presence showed the owner at
  the keyboard 7:36-8:08 and the beliefs say his wife takes the dogs out in the morning; nothing showed it happened.
  The day before at 5:13 AM it marked Thursday's 8 AM-5 PM "completed" from the clock. Milestones contradicted each
  other ("AFK 11:00 PM-4:35 AM" and "AFK for 162 min at 4:54 AM"; "Currently at keyboard", ongoing: false), carried
  over from the previous output rather than read from the activity data, and undated.
- Nothing in the record says what showed a status: daily insights cannot tell "happened (the owner said so)" from
  "the clock passed its end".

## 3. The design

### The record is objects, each claim with its evidence

- **Day events**: the Google events of the day (each its own object by its calendar id) and events the day revealed
  that no calendar held ("the daughter home sick", "the pickup moved to 8:30", "dinner out"). Each has its time and a
  lived state with what showed it:
  - `ahead` (its time has not come),
  - `happened`, `moved` (with the new time), `cancelled`, `did_not_happen` — each only with evidence,
  - `unconfirmed` — its time passed and nothing showed either way.
  Every change of state is a relation `shown_by` to its evidence: a message, a ticket reply, a presence span, a
  finished task (a finalizer judgment), a camera frame, the brain's decision on an event.
- **Milestones**: dated things that happened ("left for the school pickup 5:12 PM", "back at the computer 7:36 AM
  after 2 h 38 min away"), each `shown_by` its evidence.
- **The day theme** and **the current status**: words, rewritten each pass.

The record lives in emi.db (tables for day events and milestones, evidence as relations), not only in a resource
file; the resources the readers use today are rendered from it.

### "Did it happen?" — the record states what is known; daily insights decides what it means

The owner, 2026-10-09: "if no one did anything should it count as accomplished or not. i can see arguments for
either." The record does not decide: an event whose time passed with no sign either way is `unconfirmed`, said as
such. What that means is daily insights' judgment, where the arguments can live: a routine block (work hours, a
recurring walk) probably happened unless something says otherwise; a one-off appointment, a reminder's act, or
anything a belief would be built on needs a sign. Daily insights reads "Take out the dogs 8:00-8:30: unconfirmed
(time passed; no sign)" beside "Dinner 6:30: happened (the wife's message 6:41 PM)" and weighs them.

### Each pass works on what is new

- **Input** (owner, 2026-10-09: the main input is the concerns: "[the daughter] is out sick -> leads to [her] late
  start at school being cancelled"): the brain's picture of the day, applied to the calendar.
  1. **The open concerns as they stand now**: what each is about and what is known (the brief), with the notes made
     today, dated; never the whole journal. Applying them to today's events is the tracker's main work: a concern
     changes every event on the day that depends on it (the impact analysis the brain's facts were always meant to
     have; memory: brain association = impact analysis).
  2. **The brain's decisions today**: each event (chat, email, ticket reply, work report, presence, camera, the
     clock) in its own words, with what the brain made of it, including the ones it decided needed nothing ("the AC
     was set at 6 AM", "the Chromebook reminder was acknowledged 7:36 AM"): the day's small facts that never become
     concerns. The brain reads every signal, so the tracker reads none raw.
  3. **The record as it stands**: each event and milestone labelled, dated, with its state and what showed it.
  Presence milestones and the calendar enter by code (below). Each pass gets the record and what changed since the
  last pass (concerns changed, new decisions).
- **Output** (schema, labels only, see the object design):
  - `changes`: per event (its label K…, or "new" with a title and time) a new state and, for `moved`, the new time;
    `relies_on`: the labels of what shows it (a concern C, a brain decision B, another event); `note`: words.
  - `milestones`: new ones, each with its time and `relies_on`.
  - `day_theme`, `current_status`: words.
  A change or milestone with nothing in `relies_on` is malformed (one correction round, as elsewhere). A label in
  words is malformed.
- **Code** merges the changes into the record, writes the relations, and renders the resources for the readers.

### What code does instead of the model

- **Presence milestones**: away and at-the-keyboard spans are exact data (afk segments). Code writes them as dated
  milestones itself ("away from the computer 4:54-7:36 AM, 2 h 42 min"); the model never infers or carries them.
- **Time passing**: when an event's end passes with no state, code marks it `unconfirmed`; when its start passes,
  `ongoing` is shown to readers as "scheduled now", not as a fact.
- **The calendar**: Google events enter the record by code at the day's start and when the calendar changes (an
  event moved or deleted in Google is a change shown by the calendar itself).

### What the readers get

- **Daily insights**: the record with its evidence: each event's state and what showed it, each milestone dated
  with its source. `daily_context.md` gains the basis of every line.
- **Dayflow agents** ("today's schedule"): the same events with their lived state, labelled per their own
  projection: "Work Hours 9:00-4:30 (scheduled now)", "Dinner 6:30 (happened)". A state they read is never a
  guess.
- **The chat gate and the situation auditor**: they read concerns and the arbiter plan from this resource today.
  Those move to their own resource (or the readers read the brain directly); the day's record stops carrying them.

## 4. Order of work

1. The record in emi.db (day events, milestones, relations `shown_by`), Google events entering by code, presence
   milestones by code, `unconfirmed` by code; the resources rendered from it (readers unchanged in shape).
2. The tracker's new input (the open concerns with today's notes, the brain's decisions today, the record; labelled)
   and schema (changes and milestones with relies_on); the merge.
3. Move the concerns and the arbiter plan out of the day's resource to their readers.
4. The archive (`daily_context.md`) with each line's basis; daily insights' prompt told what `unconfirmed` means
   and how to weigh it (the owner's call on routine versus one-off).

## 5. Decisions for the owner

1. How daily insights should treat `unconfirmed`: routine blocks as probably happened and one-off events as unknown
   (recommended), or everything unconfirmed as unknown?
2. Concerns and the arbiter plan for the chat gate and the situation auditor: their own resource (smallest change),
   or those readers read the brain directly?
3. (Decided 2026-10-09: the main input is the open concerns and the brain's decisions today; the tracker reads no
   raw signal. The record and the brain read the day the same way, and every state points back to a concern or a
   decision.)
