# Concerns as state: one summary, one writer, one concern per event (2026-10-09)

Owner, 2026-10-09, reading the gate's prompt (14 open concerns, two with 40+ notes): "Why are all these concerns
polluted with stuff about things that have nothing to do with them?"; "when concern is updated it should be extremely
unlikely that any other concern is updated"; "we cannot keep a million notes under a concern and keep showing it to
the brain"; "dog walking concern from yesterday cannot still be a concern today ... let it go"; "<daughter> is valid
concern that should probably resolve tonight or tomorrow"; "we also need something that looks at these concerns and can
just summarize them. we cant have something with 100 notes on it. we also don't want to show truncated record."

Later the same day: "Concerns have an expiration date. time today that it becomes irrelevant no matter what, only
relevant for today. some other time in the future."; "This engine that builds the beliefs into the day, maybe they
should just become concerns. then really everything is a concern."; "Every concern that is created on the calendar
would have an expiri of at most 2 hours or something reasonable determined by the agent creating them, all day event
might have expiri at midnight."

Part of the hub-and-spokes pipeline (docs/design/hub_and_spokes_2026-10-09.md). Built so far: the noticer raises
issues and writes nothing to concerns (22ffbf09).

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
ids). It stops receiving: the noticer's daily dispositions (stopped 22ffbf09), the brief's `addressing:` lines, `WORK ATTACHED` /
`WORK ENDED` lines (a concern's work is read live from the work store, through the progress view), and raw ticket
JSON (a reply reaches the brain as a ticket event and is noted in words).

### 3. One event changes one concern

An event bears on one concern, raises one new concern, or needs nothing. The gate routes to one concern; the brain's
answer names one concern per event; a second is an exception the brain must state. Work is held by the one concern
it was made for (attachment by code, from the list entry), never by the brain's judgment that it "bears on" another.

### 3b. Attached work is a reference, rendered live

Owner, 2026-10-09: "attached work should just be a reference to the work object. then its rendered. that way if work
object is updated then the attached work is automatically updated." A concern holds `attached_work` as references
only: the work's id and when it was attached. Every reader renders the work from the work store when it reads it
(its objective, where it stands, each task's outcome in the finalizer's words, how it ended), through the one view
that renders work for outside readers (work_links.work_view with its progress). Nothing about the work is copied
into the concern, so nothing goes stale: persist.record_judgment and the copying in attach_work and
apply_work_outcome go; the work feedback receipts only tell the brain (rule 3's one concern) that its work moved.

### 4. Every concern expires

Every concern carries `expires_at`: the time after which it is irrelevant no matter what. Whoever raises it sets it.
- **Only relevant today** ("the movie at a friend's house tonight", "this morning's dog walk"): a time today, the end
  of its moment, or midnight. Tomorrow's walk is tomorrow's concern.
- **Relevant longer** ("<daughter> is sick and needs to get back to school", "the library fine", "the property tax by
  November 1"): a time in the future when it stops mattering even if nothing settled it.

Before that it ends as now: its done-when is met or the owner says it is over, and the brain closes it. At
`expires_at` code closes it, whatever its state, with the reason that its time passed; its open list entries are
cancelled and their work gets its end instruction, as for any closed concern (`brain_list.cancel_for_closed_concerns`).
The brain may move `expires_at` when news changes when the concern stops mattering (the appointment moved), and says
why in its note. Nothing re-asks whether a concern still matters: the clock's day-ended event
(`brain_inbox.add_day_ended_events`) goes. Stored in UTC, shown in local time like every time in a prompt.

### 5. Facts of the day are not concerns

What happened today with no matter needing attention (the AC was set, a check-in went out, the owner came back from a
visit) is a fact of the day: the brain decides it needs nothing for concerns, and the day's record keeps it.

### 6. Every concern says where it came from, in one shape

Owner, 2026-10-09: "shouldnt we have unified things like source: user." Every concern carries
`source: {kind, at, ref, content}`:
- `kind`: `user` (the owner, in chat or a ticket reply), `email`, `calendar`, `camera`, `presence`, `work`,
  `belief` (a calendar concern, rule 7), or `noticer` / `brain` for a pattern the assistant inferred;
- `at`: when it happened;
- `ref`: the event's id, kept by code, never shown in prose;
- `content`: the source's own text, copied exactly by code (the owner's message; an email's sender, subject and the
  lines that raised it; the signals the noticer counted; the belief's statement).

Readers show it alike: "raised from: the owner, Fri 1:42 PM: "..."". A concern whose source is `user` closes only
when its done-when is met or the owner says it is over; this replaces the special `owner_request` field. Each note
records the event it came from in the same way, and the summary keeps each fact's source.

### 7. The day's beliefs become concerns

Until now the day pass (`subconscious/day_pass.py`) places the owner's calendar beliefs that apply today as
placements, a second kind of input the day's tasks decide entries from. Instead each placement becomes a concern
for that day, so the day's tasks read concerns only and every outcome returns to a concern the same way.

- **The day pass creates them.** The agent that places a belief today raises its concern: what the belief asks for
  today, at what time, with the belief as its source (`kind: belief`, `content` the belief's statement) and its
  `belief_refs`, so the work's outcome still reaches the belief.
- **Identity by code.** A calendar concern's identity is its belief, its day and its time. It does not go through the
  concern door: nothing to judge, so nothing can be merged into it. (On 2026-10-02/03 beliefs did become concerns, one
  "standing" concern per belief across all days, admitted through the door; the door merged beliefs about different
  acts and one rule misdescribed them. They were retired, and about 35 still lie dormant.)
- **Expiry.** At most about two hours after its time, or what is reasonable for it, set by the day pass's agent; an
  all-day placement expires at midnight.
- **The day pass changes only its own.** When its placements change during the day it updates or closes the
  concerns it raised that day; the brain notes them like any concern.

Open: whether a calendar concern needs a planner pass (its placement already says what and when; proposed: the day's
tasks decide its entry from the concern directly); whether its entries keep the `cal:` kind or become concern entries.

## Converting what is there

- Each open concern gets its first summary from its whole record now (most are long); later summaries run only when
  the notes since grow long.
- The old notes stay in the record as history, folded into the summary and unread by anyone else.
- Work attached by the brain's judgment (not made for the concern) is detached: the dog-walk follow-up from <friend>'s
  birthday, the Edison bill from the property tax, the bedtime and lights reminders from fatigue.
- Each open concern gets its `expires_at`; those past it close.
- The dormant standing concerns from 2026-10-02/03 ("beliefs are not concerns") are removed; they hold nothing and
  every reader of closed concerns still lists them. (Stored data; asked before.)
- Each concern's `source` is its first evidence (the event it was raised from); `owner_request` becomes a source of
  kind `user`.

## Order of work

1. Readers get the brief (rule 1) and writers stop (rule 2): the noise stops reaching any model.
2. One concern per event (rule 3), attachment by code only.
3. Expiry (rule 4) and the conversion.
4. The day's beliefs as concerns (rule 7), replacing placements as an input to the day's tasks.

The work-stress concern was closed on 2026-10-09 at the owner's request (its record corrupted); fatigue keeps the
October 13 physical reminder and converts with the rest.
