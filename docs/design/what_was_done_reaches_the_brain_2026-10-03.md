# What was done reaches the brain; beliefs are not concerns (design, 2026-10-03)

Status: built 2026-10-03, uncommitted, not yet live (needs a restart; the standing concerns are retired to
dormant after it, `persist.retire_standing_concerns`). Validated against five old calendars (below). Supersedes the "placed beliefs become concerns"
half of `day_items_have_owners_2026-10-02.md` (points 2–4: owners through the door, standing concerns,
occurrence handoffs by concern). The placements half (the writer returns belief_key → time; code builds the
day) stays.

## The decision

Owner, 2026-10-03: "There is no good reason to make these beliefs into concerns. … The issue we were trying
to solve was what if we have a concern coming from the system and from operational beliefs and they overlap,
we end up doing the same thing twice. but the correct answer is not to make everything a concern. it is to
figure out what work done connects to what concerns. … we might have even within brain multiple concerns
that have overlaps. So one work object done may affect both." And: "I think there is in a brain a daemon who
is receiving from all sources information about what was done and it attaches it to appropriate concerns."

Two consequences:
1. A belief drives work directly. It is never made into a concern.
2. Whatever is done, by anyone, reaches the brain, and the brain attaches it to every concern it bears on.

## Why (what went wrong with beliefs as concerns, 2026-10-02/03)

- The concern door merged beliefs by area of life (the AC concern took in "the bedroom fan runs continuously"
  and the sleep-routine belief). Each merge changed the concern's record, the brief was written again, and its
  single rule misdescribed the other beliefs: "At 10:30 PM: Set the AC to 70°F at 9:00 PM".
- A concern was titled by its first belief, so a misworded belief (B198) became the concern's name, its brief,
  its rule's reason ("no evidence that the recurring automation is configured"), a work object ("Configure the
  Nest thermostat … each night") and a question to the owner.
- A standing concern added a door judgment, a brief and a readiness rule between "do X at time Y" and the
  work that does it; every one of today's misbehaviours came from those layers.
- Work made from a belief can already cite the belief (`belief_refs`), and its outcome already returns to the
  belief (`work_belief_feedback`, `belief_engine/work_feedback.py`). The concern added no link that was missing.

## What is done today, and how it reaches the brain (verified 2026-10-03)

- Chat and email enter the brain inbox (`subconscious/brain_inbox.py`); the gate routes each ready event to
  the open concerns it bears on, a new matter, or nothing; the brain step notes, resolves and raises
  concerns. A chat line "I booked the physical" reaches every concern it bears on.
- Dayflow's work reaches the brain only through `add_work_event`, called by concern feedback
  (`subconscious/concern_feedback.py`) for work that cites a concern (`constraints.concern_refs`): each
  finalizer judgment and each ending. The event arrives already routed to the cited concerns
  ("linked by id: the work cites these concerns"); the gate never reads it.
- So work that cites no concern (made from a belief's occurrence, a pod, an email the steward took directly)
  never reaches the brain, and work that does cite one never reaches the other concerns it bears on.

## The design

### 1. Beliefs drive work directly, from the day's calendar

- The day's calendar stays as built 2026-10-02 (`subconscious/day_pass.py`, `day_plan.py`): the selector picks
  the beliefs that change what the assistant does today, the writer returns each with the times it applies
  (`belief_key → "9:00 PM" | "all day"`), code files each placement into its hour slot, and the plan is stored as
  rows (`day_plans`, `day_plan_slots`) that `render_day` draws for every reader (the situation snapshot, the
  conversation starter, the steward's routine section). It is the source of the occurrences below. Its lines
  lose the `concern_id` owner added 2026-10-02.
- What goes on the calendar (owner, 2026-10-03): only what the assistant can act on at a time. "every morning
  at 10 jukka likes to drink coffee. thats actionable because we can remind jukka. what are you going to remind
  jukka that he likes to have his fan running?" A habit at a time of day is an item (a reminder at its time);
  a standing state, a taste, how the owner is or likes to work, is not; a belief that limits or excepts another
  is that belief's condition, never an item. The selector's prompt states this (2026-10-03).
- Modifiers, by a per-belief check against the whole catalog (decided 2026-10-03). Beliefs are stored
  separately; a calendar item's modifiers ("dog-walk times are flexible", "after the the friend's Zoom", "trash
  goes out on Wednesday nights") are other beliefs. For each selected belief the day pass asks one question of
  the whole active catalog: which beliefs change whether, when or how this one applies (its days, its timing
  or flexibility, its exceptions, what it depends on, the owner's wishes about reminders or actions for it),
  each with one sentence on how. The writer reads them with the belief as its conditions and places only the
  selected belief. Nothing is stored: it is asked fresh each day, so a wrong answer lasts one day. It replaces
  the selector's `qualifier_keys`, a guess inside a topic that missed modifiers worded differently from their
  belief and named non-modifiers (the fan as a "condition" of the AC).
  Test, 2026-10-03 (a scratch experiment, gpt-6-luna at low effort, the active catalog of 370): every expected
  modifier found, 7 of 7 — dog walk B143: B332 flexible times, B10 Fridays after Friday Night Meats, B353 stay
  close when waiting for his wife, B165 after the the friend's Zoom; trash B267: B330 Wednesday nights (and B95, a
  reminder wish, a real modifier not on the expected list); AC B149: B209 no approval needed. It over-includes
  context about the same act (the walk's gear, pace, routes); the writer reads those as context, and a missed
  modifier is the costly error. About 10K tokens per selected belief, about 250K a day.
  Build: a `dayflow_belief_modifiers` agent (its own directory: form `{modifiers: [{belief_key, how}]}`, keys
  checked against the catalog with one correction) called by `day_pass` per selected belief; the writer's
  conditions block becomes each selected belief with its modifiers and their `how`; `qualifier_keys` leaves
  the selector's form and prompt.
- The day pass hands each placement to dayflow as
  an intake item made by code: `belief_occurrence:<belief_key>:<day>:<time>`, its summary the belief's own
  words at its time ("At 9:00 PM today: the owner wants the home's AC set to 70°F by 9 PM each night …"). An
  occurrence more than an hour past is not handed over; each is handed over once.
- Work made from the item carries the belief in `constraints.belief_refs`, copied from the item by code
  (`dayflow_orchestrator/work_intake.py`, as `concern_id` is copied today). Its outcome returns to the belief
  by the existing belief feedback.
- Dismantled: placed beliefs going through the concern door (`day_pass._assign_owners`,
  `persist.record_day_beliefs`, the door's standing paragraph), the standing kind (the brain step's and the
  noticer's standing guards, the brief's standing basis and readiness rule), concern occurrences and the
  occurrence handoffs in `concern_handoff.py`.
- The standing concerns in the register (about thirty) are moved to dormant with the reason "beliefs are not
  concerns (2026-10-03)", their journals and attached work kept. This changes stored records: the owner's go
  is needed at build time.

### 2. Everything done reports to the brain

- Every finalizer judgment and every ending of every work object enters the brain inbox as a `work` event,
  whatever it cites. The concerns and beliefs it cites travel with it as a hint.
- The event is pending like any chat message or email: the gate routes it. Its cited concerns are shown to the
  gate as the ones the work was made for; the gate names every open concern the outcome bears on, or none.
  Most belief-driven work (the nightly AC set as planned) bears on no concern and costs one routing line.

### 3. The brain attaches what was done to every concern it affects

- The brain step records the outcome on each concern the gate routed it to, as it does for chat and email:
  a note of what the work established, in the owner's words where the owner replied, or a resolution when it
  settles the concern.
- When the brain routes work to a concern the work does not cite, it attaches the work to that concern (the
  work's `concern_refs` gains it through the work store, with an `attached` receipt), so the agents working on
  it read that concern's brief, and the work shows on the concern as attached work.
- The steward then sees every concern's attached work when it decides what to create, which is what keeps one
  thing from being done twice.

## Open questions

1. Gate cost: every judgment of every work object becomes an inbox event (tens a day). The gate reads them in
   pages; the brain step runs only for events routed to a concern.
2. Attaching on route: is a gate route enough to attach, or does the brain step decide it (it can see that the
   work bears on the concern without serving it)?
3. Two occurrences of the same act at one time from two beliefs: not rare. The replay below placed the
   evening dog walk from two to five beliefs a night, trash from four on Wednesday, lunch from four. One item
   per belief; the steward's prompt says several items about one act at one time are one occurrence, one work
   object citing them all. Not yet seen live.
4. The brief and the concern planner stay for matter concerns; nothing in them refers to beliefs.

## Build order

1. All work reports to the brain and the gate routes it (2): the daemon, and the fix for overlap between
   concerns, useful whatever happens to beliefs.
2. Attach on route (3).
3. Beliefs drive work directly and standing concerns are retired (1), with the owner's go on the register;
   the modifier check goes in with it (or before it, as it improves the calendar on its own).

## Validation against the old calendars (2026-10-03)

The owner: "We can look at the old calendars with the embedded beliefs, whatever we come up with here should
still produce something very similar. the old calendars were mostly good." Five old calendars (the writer's
markdown, from the agent logs: Sun 09-27 13:19, Mon 09-28 07:03, Tue 09-29 07:04, Wed 09-30 07:07, Thu 10-01
07:43) were replayed through the new selector, modifier check and writer on the same day, time and daily
context, with today's catalog (370 active). Script: a scratch replay; nothing stored.

- The same acts at the same times, every day: AC 75°F at 7 AM and 70°F at 9 PM, the morning briefing, the
  lights check at 9:30 and lights off at 10 AM, the morning and evening dog walks, the standup (Mon/Wed),
  the lunch-time work nudges and the clock-in reminder, family time at 6 PM and dinner at 6:30, a friend on
  Monday at 8 PM, the liquids cutoff, the 10:30 wind-down and the temperature check. Trash only on
  Wednesday. The modifiers carry what the old bullets said about each act (flexible walk times, who walks in
  the morning, no approval for the thermostat, no duplicate family-time notices).
- Left off, as decided: guidance tied to no moment ("keyboard presence is not proof of work", confidentiality,
  the Coinbase matter, the water bottle, video-call tips, movement breaks). Such a belief appears only as a
  modifier of an item it changes.
- Lost: the CPAP reminder at bedtime (old Mon, Tue, Wed, Thu; new only Wed); Wednesday's kids' late start at
  8:30 (selected, not placed); the 4 PM caffeine cutoff (Mon).
- Noise: one act placed from several beliefs (open question 3); a modifier selected as an item (B95 the
  reminder wish, B172 the walk's gear); the 7 AM AC placed from both B149 and B156.
- Found while replaying: the export the day pass reads was stale (549 beliefs; the store had 370, B198 retired
  but still placed), regenerated; bug list 2026-10-03. The modifier check was asked on every hourly
  regeneration; it is now asked once a day per belief while the catalog is unchanged.
