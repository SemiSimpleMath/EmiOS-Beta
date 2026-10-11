# EmiOS 2.0: beliefs rebuilt, a brain, and dayflow as its hands

Late September to 10 October 2026. This is the largest change to how the assistant thinks since the project began.
Three parts were rebuilt together, because each one's problems came from the others:

1. **Beliefs**: what the assistant knows about the household, rebuilt from the evidence up.
2. **The brain**: one place where everything that happens is read and decided, once.
3. **Dayflow**: the engine that carries work out. It no longer decides what work exists; it takes that from the
   brain and reports back.

The short version: before, several parts each read the owner's chat and email and each decided on their own what to
do. They duplicated each other, contradicted each other and fed each other's output back in as new input. Now there
is one loop:

```
everything that happens ─► the brain decides ─► dayflow does the work ─► the outcome goes back to the brain
```

The rest of this page explains each part: what was wrong, what replaced it and why.

---

## 1. Beliefs

Beliefs are the assistant's standing knowledge about the household: facts, habits, preferences, wishes ("the owner
prefers typing to voice", "the trash goes out on Tuesday nights, usually by the owner").

### What was wrong

On 2026-09-26 ten reviewer agents read all 612 active beliefs. They found:

- **Beliefs carried instructions nobody gave.** The old engine was told to make every belief "agent-ready", so a fact
  arrived with a rule bolted on: "prefers typing … default to typed interaction". The invented half told agents to
  do things that were never asked for.
- **Merging made piles.** Beliefs never merged averaged 32 words; beliefs that had absorbed 21 or more others
  averaged 89 and reached 278.
- **Duplicates came from rephrasing over time.** "I like coffee" in January and "I really love a hot cup of joe" in
  September became two beliefs. The nightly run re-read the last fourteen days every night, which made more.
- **Evidence was often about something else.** Old beliefs carried whole days of unrelated observations, and the
  original sources (which message, which reply) were lost before a belief was even formed.

### What replaced it

A new daily intake (`belief_engine/intake/`), live since 2026-09-29:

1. **Each day is read once.** An extractor reads that day's insights, chat and ticket replies and writes new beliefs.
   Each one is *atomic* (one claim that can be supported, contradicted or outgrown on its own), *declarative* (it
   describes; it never instructs) and *standalone* (a reader who never saw that day knows exactly what it means). Every
   belief cites the messages and replies it came from.
2. **Duplicates are judged, not matched.** Each new belief is compared with the beliefs closest in meaning across the
   whole catalog, and a model judges it `same`, `refines`, `contradicts` or `new`. A false merge is far worse than a
   duplicate, so the judge leans toward `new`.
3. **A contradiction revises the belief it contradicts.** The held belief is restated from all its evidence. Later
   evidence outweighs earlier, and what the owner *said* outweighs what was inferred. A report about one occasion
   ("someone else took the trash out tonight") is an exception; a statement about the state itself ("I don't do the
   trash any more") is the new state, even said once. Revisions fan out to related beliefs that the change also touches.
4. **Code writes; models judge.** Every write goes through one store, with provenance.

Before going live, the new pipeline was replayed over 185 days of history and compared belief by belief with the old
catalog: 104 better, 293 the same, 47 worse, 32 good new ones and 74 noise (13%). It covered 333 of the 360 old
beliefs the reviewers marked as worth keeping, and none of the 48 they marked to drop.

Also:

- Beliefs have short ids (`B123`). The owner can correct one on the `/beliefs` page; a correction is recorded as a
  revision with the owner's words as evidence.
- The beliefs nearest a conversation reach the chat as context.
- **The owner approves which beliefs belong on the calendar** (a daily routine, a weekly chore). Only those are placed
  on the day.
- When work is done because of a belief, the outcome returns to that belief as evidence (it may confirm it, revise it
  or retire it).

---

## 2. The brain

### What was wrong

Before the brain, "what should the assistant do about this?" was answered in several places at once:

- a dayflow **steward** read admitted chat and email and created work;
- **intake triage** decided which messages became dayflow items;
- the **noticer** wrote concerns and forced dispositions onto them daily;
- a day planner read chat and changed the day's plan, which the others then read along with the same chat.

The owner's summary on 2026-10-09: "day reads chat, brain reads day and chat, day makes a modification based on chat,
now brain has new day and same chat that day saw and could invent further things, also dayflow sees chat, context
tracker sees day and chat. its a whole mess."

The result was work made twice, work cancelled by one part and recreated by another, and concerns that grew into
logs of 40+ notes, many filed on the wrong concern.

### Hub and spokes

The brain (`app/assistant/subconscious/`) is now the hub. **Only the hub decides what work exists.** Every other part
is a spoke: it senses (reports what happened), remembers (keeps what is known) or acts (carries out what the hub
decided). A spoke never makes work of its own and never changes another part's work.

```
senses: chat, email, calendar, ticket replies, camera, presence, the clock, work outcomes, the noticer's issues
   │   each input is decided ONCE, here
   ▼
[brain step] ──writes──► concerns
   ▼   woken only by a concern changing (and the day's placements)
[the day's tasks] ──writes──► the brain's list
   ▼   woken only by an entry changing
[dayflow: intake + architect] ──writes──► work
   ▼
[finalizer] ──► the outcome, back to the brain as the result of a decision already made
```

Three rules keep it from looping:

1. Each raw input is turned into decisions exactly once, by the brain step. Other parts may *read* chat and email as
   context; none decides from them.
2. Each stage writes only its own kind of object and is woken only by the stage before it.
3. The only way back up is a work outcome, marked as the outcome of a decision already made. The brain notes it; it
   does not start new work from it.

One exception: a quick act asked for in chat ("turn on the downstairs lights") is still done at once by the chat. It
finishes and leaves nothing running, and the brain reads the chat anyway.

### The pieces

- **The inbox and the gate.** Every event becomes one row in `brain_events`, keyed by its source's own id, so it lands
  once. A gate routes each event to the one open concern it bears on, or marks it a new matter, or sets it aside (kept
  for audit).
- **The brain step** reads one matter at a time, inside its conversation, with the concern's record, related work,
  the calendar, the knowledge graph and the nearest beliefs. It writes notes, resolutions and new concerns.
- **Concerns are state, not logs** (`docs/design/concerns_as_state_2026-10-09.md`):
  - only the brain writes a concern's notes;
  - one event changes one concern;
  - readers see a summary plus the notes written since, never a long or truncated log; a summarizer folds the notes
    only when they grow long;
  - every concern expires: a matter about this evening is not still a concern tomorrow;
  - work made for a concern is attached by reference and read live, so the concern always shows its work as it stands.
- **The concern planner** looks a concern up (email, calendar, to-dos, past work) and *proposes* what work it needs.
- **The day pass** places the beliefs the owner approved for the calendar onto today. Each thing the day holds
  becomes a concern that expires a couple of hours after its time.
- **The day's tasks** is the list's **one decider**. It reads every open concern, the planner's proposals and the
  day's placements, and decides every entry on the brain's list: what the assistant should do, and when. Two deciders
  sharing one list was a source of conflicts; now there is one.
- **The brain's list** (`brain_list_entries`, with every change kept in `brain_list_changes`) is the hub's output:
  the work that should exist. Dayflow reads nothing else to decide what to do.

### Seeing it work

Every brain pass is recorded as a run, with each model call and each code decision as steps.

- **`/brain/flow`** shows the stages as a diagram, with a scrubber over the runs.
- **`/mind`** is the owner's page: the day's story, each matter, a card per open concern.

---

## 3. Dayflow

Dayflow (`app/assistant/dayflow_orchestrator/`) is the assistant's hands. Every piece of work is a **work object**:
a goal plus a small graph of steps, in a durable store with an audit log.

### What changed

- **Its only input is the brain's list.** Each planning tick, `brain_list_intake` turns every change on the list
  into work by code: a new entry becomes a new work object, a changed entry revises the goal and asks for a re-plan,
  a cancelled entry ends the work. The steward and intake triage were removed (2026-10-10).
- **The brain's end is binding.** When the brain decides something is no longer needed, its work ends. The architect
  cannot keep it alive.
- **The architect owns each goal from start to end.** It plans the steps, re-plans when told why (a failed step, the
  brain's instruction, or a goal that can no longer move), and completes or abandons the goal with its reason.
- **One judgment per role, mechanics in code.** The state mover decides whether a step's moment has come. The
  switchboard routes a step by reading its goal: tell the owner (a ticket), or do it (the worker). The finalizer judges
  whether each step's goal was achieved and writes a plain-language account of what happened.
- **Steps wait on a time or on an event.** "Remind at 9 PM" waits on a time; "when the reply arrives" waits on an
  event the state mover recognises. New: a **standing watch** ("forward every email from this sender today") runs
  each time its event happens until its end, then closes.
- **Work cites what it came from.** A step about an email carries that email's reference, not a pasted copy, so later
  steps can see what was already handled.
- **Outcomes go up.** Every judgment and every ending reaches the brain as an event, and reaches the concerns and
  beliefs the work was made for.
- **Work explains itself.** Readers outside dayflow (the brain, the owner's pages) see where work stands in words,
  including who does the next step.

---

## 4. Everything else in this release

- **Models choose by tier.** Every agent declares a tier (nano / mini / smart), which chooses the model and its
  effort.
- **Knowledge graph fail-stop.** Any failed model call stops a KG run; an outage is never read as a verdict. An item
  that fails in three runs is set aside.
- **Ids stay out of prose.** Agents see things by request-local labels; code maps labels to ids. One relations store
  holds every link between objects.
- **Standing directives by role.** The owner's standing directives reach each agent according to its role, editable
  in the UI.
- **The assistant's own mail.** Every email the assistant sends carries a mark, so when it comes back into an inbox it
  is read as the assistant's, not the owner's. This works for every account it sends from.
- **Times.** Prompts show local times; the database stores UTC. The day runs to 5 AM, not midnight.

---

## 5. Known gaps

- The cascade from a mid-day change to a re-planned step exists end to end, but has not yet been watched live.
- The line between a chat quick act and something the brain should track is drawn by the chat gate's judgment and
  needs to become more robust.
- A new list entry waits for the next planning tick (up to a few minutes), even when it is due now.
- The day's record (what the day held and what happened) runs in shadow and feeds nothing yet.

## Where to read more

- The brain: `docs/architecture/SUBCONSCIOUS.md`, `docs/design/hub_and_spokes_2026-10-09.md`,
  `docs/design/concerns_as_state_2026-10-09.md`
- Beliefs: `docs/design/belief_intake_redesign_2026-09-26.md`
- Dayflow: `docs/architecture/05_DAYFLOW.md`, `docs/architecture/08_WORK_OBJECTS.md`
- Bugs found along the way, fixed and open: `docs/design/bug_list_2026-09-18.md`
