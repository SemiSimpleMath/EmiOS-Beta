# Concerns as state: one summary, one writer, one concern per event (2026-10-09)

Owner, 2026-10-09, reading the gate's prompt (14 open concerns, two with 40+ notes): "Why are all these concerns
polluted with stuff about things that have nothing to do with them?"; "when concern is updated it should be extremely
unlikely that any other concern is updated"; "we cannot keep a million notes under a concern and keep showing it to
the brain"; "dog walking concern from yesterday cannot still be a concern today ... let it go"; "<daughter> is valid
concern that should probably resolve tonight or tomorrow"; "we also need something that looks at these concerns and can
just summarize them. we cant have something with 100 notes on it. we also don't want to show truncated record."

Part of the hub-and-spokes pipeline (docs/design/hub_and_spokes_2026-10-09.md). No code yet.

## What went wrong (root cause)

A concern is an append-only log with many writers, and every reader is given the whole log.

- **Many writers, nothing consolidates.** The brain appends a note per event; the daily noticer appends
  `disposition=keep_active` with the same paragraph every day; the brief writer appends `addressing:` lines; code
  appends `WORK ATTACHED` / `WORK ENDED`; ticket replies land as raw JSON. Nothing replaces a stale line.
- **Every reader gets all of it:** the gate, the brain step, the concern planner and through it the list decider,
  the concern door, dayflow's subconscious projection, the noticer. On 2026-10-09 the work-stress record was 46
  lines (15,233 characters), fatigue 41 (12,166).
- **Models imitate what they read.** A record full of "this does not establish X; the reminder remains scheduled"
  yields the next such note: the noise reinforces itself. Prompt rules against it are patches.
- **The concern was the brain's only place to keep anything.** A fact of the day with no concern ("the dog-walk
  follow-up was dropped") had to be filed on some concern: matter 184 (10-06) put it on a friend's birthday, because
  "used" required a concern update, and the code then attached the work, so every later report went there.
- **Events fan out to many concerns.** The gate is told to list every concern an event bears on and to err toward
  passing on; work held by several concerns reports to all of them.
- **Day matters never end.** A concern about one evening is asked again each day ("keep active") and stays.

Fixed already the same day: work not connected to a concern no longer reports to the brain (8e96d9d0); the owner's
ticket replies reach the brain directly (20afbf0f).

## The rules

### 1. Every reader gets a summary and the recent notes, never a long log

Owner, 2026-10-09: "It does not have to run on every concern every time. only when the record gets long."

What a reader sees of a concern: its identity (title, done-when, the owner's words, moment or matter), its summary,
and the notes written since the summary, in full. A summarizer folds the older notes into the summary only when the
notes since the last summary grow long; it reads the previous summary and those notes whole and writes the new summary
(what is known now, what was tried, the owner's wishes in their words, what is still open), consolidating, never
cutting. Until then the notes are shown as written. So no reader ever sees a long log or a truncated one, and the
summarizer runs only on the concern that needs it.

This replaces the brief written on every change (subconscious/concern_brief.py): the summary is the concern's state
for every reader, the concern planner included, and the planner is woken by the record changing, not by a new brief.
The old notes stay in the history the summarizer folded them from.

Readers changed: gate.py, brain_step._concern_view, concern_planner.concern_text (and so the list decider),
concern_door, pipelines/dayflow/utils/subconscious_projection.py, context_builder (the noticer).

### 2. One writer of the record: the brain

The record keeps what changed the concern, as history: the brain's notes and the events it attached (evidence with
ids). It stops receiving: the noticer's daily dispositions, the brief's `addressing:` lines, `WORK ATTACHED` /
`WORK ENDED` lines (a concern's work is read live from the work store, through the progress view), and raw ticket
JSON (a reply reaches the brain as a ticket event and is noted in words).

### 3. One event changes one concern

An event bears on one concern, raises one new concern, or needs nothing. The gate routes to one concern; the brain's
answer names one concern per event; a second is an exception the brain must state. Work is held by the one concern
it was made for (attachment by code, from the list entry), never by the brain's judgment that it "bears on" another.

### 4. A concern says what ends it

- **A moment** ("the movie at a friend's house tonight", "this morning's dog walk") carries an end time; code closes it then,
  with the reason "its moment has passed". Tomorrow's walk is tomorrow's concern, raised by tomorrow's events.
- **A matter** ("<daughter> is sick and needs to get back to school", "the library fine", "the property tax by November
  1") ends when its done-when is met or the owner says it is over; the brain closes it when news says so. No daily
  re-asking: the clock's day-ended question and the noticer's daily disposition go.

The brain chooses moment or matter when it raises a concern, and may change it.

### 5. Facts of the day are not concerns

What happened today with no matter needing attention (the AC was set, a check-in went out, the owner came back from a
visit) is a fact of the day: the brain decides it needs nothing for concerns, and the day's record keeps it.

## Converting what is there

- Each open concern's brief is rewritten once from its whole record, under the new writer rules.
- The old notes stay in the record as history, unread by anyone but the brief writer.
- Work attached by the brain's judgment (not made for the concern) is detached: the dog-walk follow-up from <friend>'s
  birthday, the Edison bill from the property tax, the bedtime and lights reminders from fatigue.
- Each open concern is marked moment or matter; moments from before today close.

## Order of work

1. Readers get the brief (rule 1) and writers stop (rule 2): the noise stops reaching any model.
2. One concern per event (rule 3), attachment by code only.
3. Moments and matters (rule 4), and the conversion.

The work-stress concern was closed on 2026-10-09 at the owner's request (its record corrupted); fatigue keeps the
October 13 physical reminder and converts with the rest.
