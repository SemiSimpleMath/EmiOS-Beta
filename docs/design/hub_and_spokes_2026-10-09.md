# Hub and spokes: one decider, everything else senses, remembers or acts (2026-10-09)

Owner, 2026-10-09: "Really we need a single giant input decider that makes concerns out of them, somehow we
ultimately get tasks out of them. Then when something happens in the brain that would have an affect on a dispatched
task, it cascades down. Conversely when tasks are done their effect flows up and concerns are updated." Of the two
mental models (the brain as a hub with helpers around it, or the brain as one large standalone thing): "hub and spokes
sounds more reasonable."

This document maps the system onto that shape, says what already exists, and names what is missing. Owner,
2026-10-09, after the pipeline below was set out: "Yes code all this." Parts 1-4 are built (fac5b8fc, 65160d69,
bbf4ac77, 1df46f7c); each gap below says where it stands.

## The rule

**Only the hub decides what work exists.** Every other part is a spoke: it senses (reports what happened), remembers
(keeps what is known), or acts (carries out what the hub decided). A spoke never makes work of its own and never
changes another part's work. It tells the hub, and the hub changes its list.

One exception: a quick act from chat ("turn on the downstairs lights", "research duck migrations", "put these five
things on the calendar") is done at once by the chat gate's hands. It is one act that finishes, leaves nothing
running, and the hub still sees it: it reads the chat, and the calendar comes back to it as a sense. Anything that
changes what happens later ("don't send the encouragements for two hours", "remind me tomorrow at 9") goes only to
the hub. Owner, 2026-10-09: the line between the two must be solved robustly at some point; this design assumes it
can be.

## The pipeline

Owner, 2026-10-09, on the mess it replaces: "day reads chat, brain reads day and chat, day makes a modification based
on chat, now brain has new day and same chat that day saw and could invent further things, also dayflow sees chat,
context tracker sees day and chat. its a whole mess." The trouble is not who reads what: more context is good, and
every part may read anything. It is who reacts to what.

```
inputs (chat, email, calendar, ticket replies, camera, presence, the clock, work outcomes)
   |  each input is decided ONCE, here
   v
[brain step] --writes--> concerns
   v  woken only by a concern changing (and the day pass's placements)
[list decider: day_tasks] --writes--> the brain's list
   v  woken only by an entry changing
[intake + architect] --writes--> work
   v
[finalizer] --> outcome --> back to the inputs, as the outcome of a decision already made
```

1. **Each raw input is turned into decisions exactly once, by the brain step.** Every other part may read chat and
   email as context; none decides from them.
2. **Each stage writes only its own kind of object, and is woken only by the stage before it.** The list decider is
   woken by a concern's record or plan, or by the placements; never by chat, the rest of the situation, or the work
   its own entries made (`day_tasks.fingerprint`).
3. **The only way back up is a work outcome, marked as the outcome of a decision already made.** The brain notes it;
   it does not start new work from it.

Each stage writes only when its output differs (a rewording is not a change), and names what it consumed in
`relies_on`. Outside the pipeline: the chat gate's quick acts (one act that finishes; the hub reads the chat, and the
calendar returns as a sense); dayflow releasing a step waiting on an arrival or holding one for the moment (when its
own steps run, never whether work exists); the day's record, which reads everything and decides nothing, feeding
daily insights and so tomorrow's beliefs, a loop that runs once a day by design.

## The map

| Part | Role | Where | State |
|---|---|---|---|
| Brain inbox + gate | sense intake: routes every event to the concerns it bears on | `subconscious/brain_inbox.py`, `gate.py` | exists |
| Brain step | hub: one matter at a time, decides what each event changes in the concerns | `subconscious/brain_step.py` | exists |
| Concern planner (`concern_tasks`) | hub: looks a concern's matter up, proposes its entries | `subconscious/concern_planner.py` | built (gap 1) |
| The day's tasks (`day_tasks`) | hub: the list's one decider: every entry, the calendar's, the day's and every concern's | `subconscious/day_tasks.py` | built (gap 1) |
| The brain's list | the hub's output: what work should exist | `subconscious/brain_list.py` | exists |
| Chat | sense | brain_inbox `ingest_chat` | exists |
| Email | sense | `ingest_email` | exists |
| Camera | sense | `ingest_camera` | exists |
| Presence | sense | `ingest_presence` | exists |
| Clock (a concern's day ended) | sense | `add_day_ended_events` | exists |
| Work outcomes | sense (the cascade up) | `add_work_event`, concern and belief feedback | exists |
| Calendar | sense | `ingest_calendar` | built (gap 2) |
| Ticket replies | sense: the owner's answers to reminders, notices and questions (dayflow's own return through work reports) | `ingest_tickets` | built 2026-10-09 (20afbf0f) |
| Day record | sense: what the day held and what happened | `subconscious/day_record.py` | shadow; feeds nothing yet |
| Concerns | memory | concern register | exists |
| Beliefs | memory | belief store; placed by the day pass, and by meaning to the brain step and the list decider (`belief_links`) | built (gap 3) |
| Knowledge graph | memory | `kg_links` in the brain step | exists |
| Dayflow | hands: makes work from the list, plans how, carries it out | `dayflow_orchestrator/` | exists |
| Chat gate + switchboard | hands for quick acts | master_room | exists; prompt fixed (gap 6) |
| Devices, calendar writes, email sends | hands, through dayflow or the switchboard | tools | exist |

## The cascades

**Down (a fact changes, in-flight work follows).** This exists:
1. An event reaches the brain. The brain notes it on a concern (or raises one).
2. The concern's brief changes, so the planner plans it again and proposes; the list decider modifies or cancels
   the entries.
3. `brain_list_intake_node` revises the work and leaves the architect a `replan` instruction (modify), or an `end`
   instruction (cancel), in the same commit.
4. The architect re-plans. It may prune queued or wake-held steps because the instruction licenses it.

A calendar or day entry follows the same path from step 3 when the day's tasks rewrites it. Since d06ab78b
(2026-10-09) that includes an earlier day's entry whose work is still in motion. That fix is committed but not yet
running (restart pending) and not yet observed. A step already running is not stopped: ending work revokes its
further writes, not its thread.

**Up (work is done, concerns and beliefs hear it).** This exists: every finalizer judgment and ending becomes a work
event in the brain inbox, and work attached to a concern or carrying a belief delivers its outcome to that concern or
belief by code. Since 2026-10-09 a report on work made from the brain's own list says so, so the brain notes it
instead of raising a concern from it.

## What is missing

### 1. Two deciders write the list, and neither can change the other's entries

`concern_tasks` writes a concern's entries (`con:`). `day_tasks` writes the calendar entries (`cal:`, from placed
beliefs) and its own day entries (`day:`). Each can modify or cancel only its own.

The encouragement case shows the conflict. The owner says in chat: "don't bother me with these encouragement updates
for the next two hours."
- The brain reads it and raises a concern carrying the owner's words.
- The concern's planner can only add a `con:` entry ("pause the encouragements"). That becomes new work whose worker
  is asked to stop other work, which no tool reaches. That is the same failure as the 10:20 check-in re-time on
  2026-10-09 (bug list).
- The hourly encouragement is a `cal:` entry from a belief, so only `day_tasks` can modify it, and only if it reads
  the concern and chooses to.

So the outcome depends on which decider acts first, and both may act.

The fix is one decider over the whole list. **Recommended:** the day's tasks becomes the hub's single list decider.
It already reads every open concern in full, the brain's decisions today, the placed beliefs, and every open entry.
It would write every kind of entry, and modify or cancel any of them. The concern planner keeps its lookups and its
decision (act now, hold until, no action) as facts on the concern, and stops writing entries. Multi-day concerns need
an answer: the day's tasks decides today. Either it also decides entries for later days, or a concern's future-dated
entries stay with the planner and the day's tasks may still modify them. **Owner decision.**

**Built 2026-10-09 (fac5b8fc).** The day's tasks decides every entry, a concern's for any date included
(`brain_list.apply_entries`; `apply_concern` removed): a new entry serving a concern is the concern's (`con:`), one
serving none is the day's (`day:`), and any entry given may be modified or cancelled by its label. The planner keeps
its lookups and its decision and stores the changes it proposes on its plan (`plan.proposals`); the decider reads them
by label beside the concern's entries and the ones cancelled. Its fingerprint counts only the placements and each open
concern's record and plan. A day not yet placed still decides its concerns. Also built: every decider sees what an
entry's work is still waiting for (`work_view.progress`, `entry_work.j2`), and an arrival that work is waiting for is
that work's to act on.

### 2. The calendar is not a sense

The inbox has chat, email, camera, presence, clock and work. A calendar event added, moved or cancelled wakes nothing.
The brain sees the calendar only as context when some other event arrives, and the day pass reads it on the
ten-minute heartbeat. A calendar change is a fact that can bear on concerns ("the dentist moved to Thursday") and on
entries. Needed: calendar changes as brain events (added, moved, cancelled), keyed by the event's id and its version,
routed by the gate like any other event. A quick act that adds calendar events then reaches the hub through this.

**Built 2026-10-09 (bbf4ac77).** `brain_inbox.ingest_calendar` compares the synced week with what it last saw: added
(created since the last look), on the calendar this week (made earlier, now in the week), changed (with what it was),
removed (gone while still ahead). Another occurrence of a known series is not a change; the first look only records. A
calendar `repo_update` wakes the brain. Dayflow is still poked by the same update; its tick decides no work.

### 3. Beliefs are not context for the hub

Beliefs reach decisions only as the day pass's placements. The brain step does not read them, and neither does the
day record. On 2026-10-09 the record inferred that the owner did not take the dogs out without the belief that
someone else sometimes does. Needed: the beliefs closest in meaning to the matter, as context (facts, never
instructions) for the brain step and the list decider, the same way the knowledge graph's facts are given now.

**Built 2026-10-09 (65160d69)** for the brain step (nearest its events and concerns) and the list decider (nearest the
open concerns): up to 8 beliefs at 0.25 cosine or more, calibrated on the live store. The day's record does not read
them yet (owner, 2026-10-09: no record fixes for now).

### 4. The cascade into in-flight work is unobserved

Steps 3 and 4 of the cascade down exist, and the d06ab78b fix lets an entry reach work made from it on an earlier day.
Nobody has yet watched a mid-day change go from chat to a re-planned step. That is the first acceptance test below.

### 5. A request about what happens next waits for the room to go quiet

The brain reads a room's chat once it has been quiet for five minutes (`brain_inbox.QUIET`). Then come the planner,
the next planning tick and the architect. For "stop for two hours", one more encouragement may go out first. Possible
fix: when the chat gate acknowledges a "track this" request, it marks the room ready now. Only if the acceptance test
shows the delay matters.

### 6. The chat gate's prompt describes a retired path

It still offers "delegate to dayflow orchestrator" and speaks of "dayflow delegations". It does not say that a change
to what happens later is "track this", never a switchboard handoff. Recorded in the bug list (2026-10-09).

**Fixed 2026-10-09 (1df46f7c).**

### 7. Leftovers of the steward era still run

Both lanes are live and the steward is retired, but each brain pass still calls `run_handoffs` and
`run_occurrence_handoffs`, which return early on a live lane. Intake triage still runs, and an item it admits has no
reviewer. These are dead paths to remove once the hub owns everything. They are not urgent.

**Not done; owner decision.** They are dead only because `configs/brain_lanes.json` has both lanes live and the
steward retired, and those switches are the way back to the steward. Removing the two stages alone would leave the
switches silently broken. Removing them properly retires the steward and the lane switches together: the steward's
prep and persist nodes, `work_intake`'s handoff branches, the scheduler's `concern_handoff` event, intake triage, and
the readers of handoff items (`brain_stories`, `routes/brain_debug.py`).

## Acceptance cases

1. **Encouragement pause.** The owner says "no encouragement updates for the next two hours" at 1 PM. Expected: one
   decision, made once. The hourly encouragement entry is modified (none until 3 PM) and no new work is made. The work
   is re-planned so the 2 PM step does not go out, and the 3 PM one does. Nothing asks a worker to stop other work.
2. **A quick act clashing with scheduled work.** The owner says "set the AC to 72" at 4 PM, and scheduled work sets it
   to 75 at 6 PM. The switchboard does it at once. The brain reads the chat and the list decider decides whether the
   6 PM step still holds. The owner is asked only if it is unclear.
3. **Someone else did it.** The dogs were taken out by someone else. With the belief that this happens as context,
   the record and the decider do not conclude that it did not happen, and the owner is not reminded.
4. **A plan changes during the day** (Friday Night Meats moved or cancelled in chat, or on the calendar). The entry is
   modified, and its reminder work is re-planned or ended. It arrives through gap 2 when only the calendar changed.

## Order of work

Done 2026-10-09: gaps 1, 2, 3 and 6. Next:
1. Restart, then observe the cascade (gap 4) with case 1 or 4 when it happens naturally, and the list decider's first
   runs as the only writer.
2. Gap 7 when the owner decides the steward's rollback path can go.
3. Gap 5 only if case 1 shows the delay.
