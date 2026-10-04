# One work engine, many front doors (design, 2026-10-03)

Status: proposed 2026-10-03, no code. Written from a design conversation with the owner after the
build of `what_was_done_reaches_the_brain_2026-10-03.md`. It generalizes that note's "daemon" (everything
done reports to the brain) and replaces, in the long run, the belief-derived calendar with commitments.
Each section marks what exists in the code today and what is new.

## Why

Found on 2026-10-03, while building and checking the belief calendar and the brain's work reports:

- **Chat acts are invisible.** The master room acts directly (`master_room_manager` → `emi_team_manager`
  and its tools). No work object, no receipt, no report. The brain's chat intake takes the owner's
  messages only (`brain_inbox.ingest_chat`, `role='user'`), so an order the assistant placed in chat
  reaches no concern unless the owner's own words say enough.
- **Nothing in code stops one act being done twice.** Dayflow guards against duplicates by reading its own
  records: the steward's portfolio, completed and dropped work. Chat acts are not among them. The brain
  is told to raise what the owner asks to have done later; if chat also starts it, two copies run.
- **Deferred chat requests take five model stages.** The master room's chat gate detects "watch for an
  email from X" (`track_tf`) and only acknowledges it. The delegation it used to write was retired
  2026-09-30 (`master_room_chat_task_router_node.py`). Today the request becomes work only through the
  brain: room goes quiet, gate, brain step raises a concern, brief, concern planner, handoff, steward,
  architect. An email in the first hour can be missed, and the brain can judge the request differently
  from how it was asked.
- **Recurring behaviour is re-derived every day from loosely worded beliefs.** Selector, modifier check and
  writer place the day's beliefs each morning. Replayed against five old calendars (2026-10-03) it put the
  right acts at the right times, but placed one act from several beliefs (the evening dog walk from up to
  five), selected modifiers as items, and once kept placing a retired belief from a stale export. A
  misread belief (B198) turned "just do it every night" into a request to set up a device schedule.
- **Dayflow is one bundle.** One planning tick runs intake triage → steward → architect → state mover →
  materializer → action selector → switchboard → dispatch, and every planning tick and wake holds one
  lock (`DayflowScheduler._run_gate`). Nothing outside it can create and run a work object.

## The shape

Four records, and one way to act.

| Record | What it is | Today |
|---|---|---|
| Events | What came in: messages, emails, sensor readings, calendar changes. Append-only. | the brain inbox (`brain_events`) and `unified_log_2026` |
| Memory | What is known: facts (the knowledge graph), the owner's preferences and patterns (beliefs). Never acts by itself. | the knowledge graph, the belief intake store |
| Work objects | What should become true, with its plan and its record. | the work store; dayflow only |
| Acts | What the assistant did, with provenance, outcome and refs. | dayflow's execution receipts (attempts and calls); no refs; nothing for chat |

Above work objects sit **concerns** (the brain's: a matter in the household's life, closed by a state of
life, not by an act) and **commitments** (new: the owner's standing wishes that generate work).

```
Concern (why)        brain         "sleeps well"                   done-when: a state of life
   │ many-to-many, by reference
Work object (what)   work store    "AC to 70°F tonight at 9 PM"     done-when: an outcome
   │ 1..N, through its nodes
Act (did)            choke point   "set Nest 70°F, 21:00:04, ok"    outcome, refs
```

## 1. The work engine

One module owns a work object from creation to its end. Dayflow and the master room share it.

| Step | What it does | Today |
|---|---|---|
| Create | A work object with its provenance (trigger, originator, source ids), links (concerns, beliefs, commitment) and a duplicate key. | `work_store` `create_work_object`; called only by dayflow (`work_persist`) |
| Plan | The architect builds and revises the graph. | `work_architect`, inside the planning tick |
| Schedule | Nodes wait on a time (`wake_at`) or an event (`wake_ref`); at each wake, "does this still make sense now?" | time wakes: `DayflowScheduler`; event wakes: a prose condition the state mover matches against intake (`05_DAYFLOW.md`); the check: the state mover |
| Execute | Workers act through the choke point (section 3). | `work_emi_team_manager`, dispatched one node per pass |
| Judge | The finalizer's verdict, outcome account and recommendations. | `WorkFinalizerNode` |
| Re-plan or end | The architect again. | exists |
| Report | One return path: to the originator and to the brain. | to the brain since 2026-10-03 (`concern_feedback`); no originator |

What changes:

- **The engine is a service, not a pass.** Front doors call it; it is not reached only from inside a
  planning tick. The architect runs for one work object when that object needs planning, not as a stage
  of the tick.
- **One lock per work object, not one for everything.** `_run_gate` lets one planning tick or wake run at a
  time; it also protects the one-dispatch-per-pass rule and the dispatch-epoch fences. Those have to hold
  per work object instead, so chat can create and run one without waiting behind a tick, and unrelated
  work objects progress side by side. This is the deepest change.
- **An originator on every work object.** Where it came from (`master_room` and the source message, the
  steward, a commitment), and so where its progress, questions and ending are reported.

## 2. Front doors

Front doors decide that work should exist. They never plan or execute.

| Door | How it decides | Example |
|---|---|---|
| Chat | The owner asked: no judgment needed. | "order lunch"; "watch for email from [a friend] today and text me" |
| Steward | Judges open-ended intake. | an email that needs a reply; a concern the brain handed over |
| Commitments | Code, per occurrence or per triggering event. | "AC to 70 at 9 PM"; "when [the owner] leaves for lunch, email [a family member]" |
| Brain | Through the steward, as concern handoffs (exists). | "the physical is still not booked" |

### Chat

The owner (2026-10-03): "[the assistant's] chat can start a work object, this object carries its own provenance to the
chat, it can record its originator as the master_room so that we know that any progress on this wo is
reported to master room. its a work object so we can deposit right into dayflow as active wo being worked
on nothing will pick it up since its active. then the wo can return through finalizer and now essentially
becomes a fully fledged wo in dayflow if it still needs completing."

- **A request done now** ("order lunch"). The master room plans and acts as it does today. When the first
  act that changes something reaches the choke point, code creates the work object through the store's
  transactional apply, never through a dayflow pass: objective = the owner's words, originator =
  `master_room` + the message id, one main node claimed by the chat's run. The node is `dispatched` with an
  attempt receipt held by that run, so dayflow cannot start it again (receipts already block replacement
  execution while an outcome is unresolved). If the chat run dies, `sweep_stuck_work_nodes` records a
  failed result after 80 minutes of inactivity, as it does for dayflow.
- **When the turn ends, the finalizer judges it**, after the reply is sent so the owner never waits.
  Achieved: closed. Retry, new approach, plan changes: the node goes to `failed`, the architect is woken,
  and from there it is an ordinary dayflow work object. Unrecoverable or ask the user: the existing paths.
- **A request that runs over time** ("watch for email from [a friend] today; text me if one comes"). The
  master room creates the work object unclaimed, queued for the architect, and replies "watching". The
  architect plans a node waiting on that email's arrival. This brings back the direct handoff retired on
  2026-09-30, now as a proper work object; the brain still sees all of it, as work events.
- **Read-only turns** ("what's on my calendar") create nothing.
- The chat's run carries the work-object owner context (`current_owner()`), like dayflow's dispatch, so its
  acts are recorded against the work object. Its scope stays the master room's.

### Steward

It keeps its job of judging open-ended intake (emails, concern handoffs, pods), and loses the job of
inventing the day's routine work: commitments generate that by code.

### Commitments

The owner's standing wishes about what the assistant does on its own:

| Field | Holds |
|---|---|
| what and when | "AC to 70°F at 9 PM nightly", or a triggering event ("when [the owner] leaves for lunch"), with structured exceptions ("Thursday after a Monday holiday") |
| why | the concern it answers (sleep) and the belief or evidence behind it |
| control | suggest, remind, or do it |
| approved | the owner's words and when, each time control went up |
| status | active, paused, retired (with the owner's words) |

- **Code turns each commitment into a work object per occurrence**, keyed by the occurrence
  ("ac-night/2026-10-03/21:00"). A time trigger becomes a node with `wake_at`; an event trigger becomes a
  node waiting on that event. The judgment left at each occurrence is narrow: does it still make sense now
  (away, sick, already done in chat)? That is the state mover's question today.
- **Event triggers need new work.** Event wakes today match prose against intake (emails, pods); sensor
  events (going AFK, a location change) are not intake. "Leaves for lunch" also needs a reliable signal:
  AFK detection has been seen to go blind during a fullscreen game (2026-09-17).
- **Beliefs never generate acts by themselves.** They inform how an act is done (today's modifiers: walk
  times are flexible, no approval needed for the thermostat) and whether one is needed. A belief that would
  change whether or what an act is becomes a proposal to change the commitment.

#### Where commitments come from

The owner (2026-10-03): "The magic of [the assistant] was though that i had been complaining about bad
sleep, [the assistant] asked what was going wrong i said, well i wake up hot and sweaty and next night she
popped up a recommendation to turn the AC down at 9PM then later when i gave her control she started to do
it by herself."

```
events → beliefs (inferred, no approval) → proposals → commitments (approved) → work objects → acts
```

- **Concerns lead to questions and proposals.** A concern (bad sleep) leads the assistant to ask, the answer
  becomes a belief (wakes hot), and the proposal answers the concern ("turn the AC down at 9 PM"). The
  commitment keeps that origin.
- **Approval is given in conversation, in steps.** First a suggestion, then "remind me", then "just do it".
  Each step up is recorded with the owner's words. No settings page is needed.
- **The concern stays open after the act starts.** It closes when sleep is better, not when the AC was set.
  That lets the assistant adjust ("still waking hot? try 69") or stop in the cold season.
- **Patterns the assistant notices** are proposed with their evidence ("you coded past 11 three of the last
  four Monday nights; a wind-down nudge at 10:15?"). A decline is recorded as the owner's ruling and not
  proposed again unless something new outweighs it.
- **Beliefs stay free.** The belief system's point is to know the owner better than he knows himself, and
  approving each belief would end that. Approval sits only where the assistant starts acting on its own,
  and each time control goes up. The owner: "no one wants to daily edit their assistant."

## 3. The choke point and the act ledger

Every act that changes something in the world goes through one executor, from any door.

| Field | Holds |
|---|---|
| act id | its identity |
| work object and node | the work it served (chat acts included, through their work objects) |
| what | the action in words, the tool and its arguments |
| who asked | the owner's message, a commitment's occurrence, a concern's plan |
| path | the invocation tree (master room → emi_team → devices) |
| authority | the control level it ran under |
| when | started, finished |
| outcome | succeeded, failed, refused, unknown |
| refs | what it produced: a confirmation, a sent message's id, an event id, a pod |
| verified by | the finalizer's verdict, "the owner was present", or nothing yet |

- **Written twice:** "about to do X", then the outcome. An act that never reports back stays "unknown",
  which blocks a replacement attempt, as the receipts do today. Corrections are new records.
- **Today:** dayflow's two execution-receipt tables (attempts and calls) cover dayflow's nodes, without refs
  or provenance. The ledger is those receipts extended to every door.
- **Start small (the owner, 2026-10-03):** "i would just start with master rooms emi_team. emi_team already
  prepares at least the high level artifacts. lets start small if we need like exact ids of calendar
  events etc. we can add that later." The first version takes the final answer of `emi_team_manager` when
  the master room calls it, with the manager's exit kind (a `final_answer` alone does not prove success:
  the default error exit uses that type too, `02_MANAGERS.md`) and the provenance. Exact refs from tool
  results come later.

## 4. Duplicates

Things are linked by where they came from, not by wording:

- **Duplicate keys at create.** A commitment occurrence and a concern part carry a key made by code; the
  engine refuses a second open or finished work object with the same key. A chat request is matched to an
  existing work object by a model once, when the work object is created.
- **Source message ids, checked at three points**, so the brain never raises a chat request a second time:
  1. A work object made from a chat message records the message id. When the gate passes that message to
     the brain, code annotates it: "taken up as work W (active)". The brain may note it on a concern or
     attach W, and raises nothing new for that request.
  2. If the brain raises a concern anyway and its evidence includes a message that became work, code
     attaches that work at once; the concern starts addressing and is not handed to dayflow while it is
     open.
  3. When work is about to be created from a concern handoff, code follows the evidence to its source
     messages; if one already became a work object, the new one is refused and the concern points at the
     existing one.
- **At a wake, the ledger is read.** A waiting node that was done elsewhere ("set the AC in chat at 8:40")
  is closed by the state mover as already done.

## 5. Concerns and work objects

- **By reference both ways, many-to-many.** A work object cites the concerns it serves; a concern lists its
  work by id. Today the concern also copies the work's title, status and judgments (`attached_work`), and
  the copy goes stale: on 2026-10-03 the AC concern still showed `work_fd405350a76f` as active after it was
  abandoned. The concern keeps the ids, and the brief reads status and verdicts from the work store when it
  is written.
- **Reports from every door reach the brain** as pending events; the gate routes them to the concerns they
  bear on; a work object the brain cites on a concern is attached to it (built 2026-10-03 for dayflow work:
  `brain_step._attach_cited_work`). With the engine, the work object also gains the concern
  (`concern_refs`), so its agents read that concern's brief. That side is not built.
- **A concern's plan becomes parts.** The concern planner already writes the things that should be done.
  Each part a door takes up becomes a work object; "in progress" means that part's work object is open,
  replacing today's single "addressing" flag. Several modules can work one concern, each on its own part.
  The owner (2026-10-03): concerns "can be taken up by other modules as long as they are not reproducing
  work, each module that adopts a concern can do their part."
- **A work object being done never closes a concern by itself.** The brain compares the concern's done-when
  with what happened.

## 6. Reporting

- **To the originator.** A chat work object's finalizer outcome, its questions and its ending appear in
  the chat where it was asked, not as a separate ticket. Dayflow's own work reports as today.
- **To the brain.** Every judgment and ending is a pending event (built 2026-10-03), and with the act
  ledger every act can be one.

## What exists, what is new

| Piece | Exists | New |
|---|---|---|
| Work store, graph, architect, workers, finalizer | yes, dayflow only | called by every door; an originator; duplicate keys |
| Execution receipts | yes, dayflow only | every act, with provenance and refs |
| Time wakes | yes | — |
| Event wakes | prose matched against intake | sensor events (AFK, location) |
| One lock (`_run_gate`) | yes | one lock per work object |
| Chat acts | direct, unrecorded | through the choke point, as work objects |
| Deferred chat requests | acknowledged; reach dayflow only through the brain | a work object made by the master room |
| Commitments | none: beliefs placed daily (`day_pass`) | their own record; work per occurrence |
| Concern ↔ work | concern-side copy, dayflow work only | references both ways, any door |
| Work reports to the brain | built 2026-10-03, dayflow work | every door |

## Open questions

1. **When the finalizer judges a chat act.** After the reply, always? Or only when the act did not plainly
   finish? And does it read the owner's next message ("that's not what I meant") first?
2. **Matching a chat request to an existing work object.** One model call when it is created, or only for
   requests the chat gate marks as possibly in progress elsewhere?
3. **Claims per part or per concern.** Per part needs stable ids in the concern planner's output.
4. **Releasing a claim.** Done, failed, or a time limit, so a claim held by a module that died does not
   block a concern.
5. **Volume.** Many small chat work objects a day: the steward's completed list and the brain's work events
   grow. Limit what each reads to what bears on it.
6. **From beliefs to commitments.** Migrating today's recurring beliefs (AC at night, the morning routine,
   the trash night) into commitments the owner approves, and the day pass stopping once they exist.
7. **The per-work-object lock.** How the fences that rely on one pass at a time (one dispatch per pass,
   dispatch epochs) hold when passes run side by side.

## Suggested build order

1. **Chat reports what it did** (the smallest step): when the master room's `emi_team_manager` call ends,
   its final answer, exit kind and provenance go to the brain inbox as a pending event. No work object yet.
2. **Chat creates work objects** through the store, with an originator and the source message id; requests
   that run over time are handed to dayflow as work objects; the brain annotates messages that became work.
3. **The choke point and the act ledger** at the tool-execution layer, for every door.
4. **Commitments**, proposed from concerns and beliefs, approved in conversation; work per occurrence by
   code; the day pass retires once they cover the recurring acts.
5. **The engine as a service** with a lock per work object; the steward becomes a front door.
