# Objects and projections: how ids, labels and relations flow (design, 2026-10-08)

Status: DESIGN for the owner's review. No code yet.

Owner, 2026-10-08:
- "each prompt needs to be a projection of these objects you create and objects have id and then data fields"
- "ids identify objects they are not part of the data"
- "short labels are never written into the prose. you have to have the schema write the pairs with ids separate
  from other data."
- "schemas need to have way of capturing what object the model is talking about. or what objects it wants to
  attach to a concern"

## 1. Why

Read-only inventories of every brain and dayflow agent (2026-10-08) found the same failure everywhere: what a thing
*is* (its id), what it *says* (its data) and what it is *related to* (other objects) are mixed into one text.

- Raw ids are shown to models as content and copied back by hand: the architect (node ids, depends_on, abandon,
  duplicate_of), state_mover (`work_x::slug--abc123`, matched as exact strings, a typo silently promotes instead of
  holding), action_selector (matched by substring), intake_triage, the worker planner ("preserve exact ids"), the
  ticket planner, the daily context tracker, the noticer (2 of 29 concern ids it emitted did not exist: a miscopied
  uuid and a title), the routine writer (`belief_key`).
- Labels leak into stored prose and come back meaningless: list entries' `why` ("W21 ... W29": tonight's phone-charge
  reminder lost), brain reasons and notes ("C1 remains open"), brief fields ("(S13; S14)"), door reasons, gate
  reasoning. 150 of 427 list changes, 162 of 515 briefs.
- Code writes ids into stored prose: journal lines "WORK ATTACHED work_x" (226 of 911 lines), work reports
  `Work "<title>" (work_x)`, "duplicate of <id>", belief evidence "work <id> satisfied", the brief rendered into
  `constraints.rationale` with "(entry cal:...)", and since e2c260f2 the objective itself with dates baked in.
- Relations exist only as prose: an ending "already achieved in the recently completed work '<title>'" (the 10-08
  phone reminder), "Superseded by work_a7af...", directives naming a slug to read ("recorded in research_funny_fact",
  some not matching the real id), findings citing pods in prose, `cites` validated and then dropped.
- The same object has several identities: a concern is a uuid, `concern:<prefix>`, `concern 3e8f2c71`, C# in four
  different prompts; a work object is `work_x`, `work:work_x:receipt`, W#, S#, a title.
- One field did two jobs: the brain step's cited `events` both cited and attached, so citing the Nest event "to say
  it does not apply" attached the Nest work to the pickup concern.

## 2. The model

**Objects.** Every kind of thing is an object: an `id` (identity only, minted by code, never shown to a model, never
inside text) and data fields (text, times as UTC data, status). Text fields hold words only.

**Relations.** Every link between objects is a typed relation row, held by code: `(from, rel, to, at, by)`, where
`from`/`to` are object refs (kind + id) and `by` is who asserted it (an agent run, code, the owner). One store for
all of them (`relations` in emi.db), not ad-hoc fields in JSON blobs. Existing structured links (work-store
dependency edges, `list_entry_id`, `concern_refs`, `belief_refs`, `attached_work`) either stay where they are and are
read through the same interface, or move into it; decided per object in the build.

**Projections.** A prompt is rendered by code from objects. A `Projection` per agent call:
- `label(obj)` gives the object a short label for this call (kind prefix + number: W3, C1, T2, M5), the same object
  always the same label within the call;
- templates render an object through one macro per kind: its label and its data fields (times through `local_time`),
  never its id;
- related objects are rendered by following relations at render time ("relies on: [W3] Remind ... (abandoned Thu
  10-08 05:13 AM: ...)"), so a stored reason's references come back as live, dated objects;
- `resolve(labels, kinds)` maps a model's labels back to objects, refusing unknown labels or wrong kinds (a malformed
  answer, like a missing field; not a judgment).
Labels live only inside one call. Nothing stores a label.

**Output schemas.** Every reference a model makes is a field of labels, named by the relation it asserts, separate
from prose:
- `about`: what a statement or decision concerns;
- `attach`: objects to link to a concern (work, a message, an email), a deliberate act, distinct from reading;
- `relies_on`: what a reason rests on (replaces "cites", "new_since", "known.source");
- `covers` / `covered_by`, `duplicate_of`, `superseded_by`, `ended_by` (the architect's "already done by that work");
- `depends_on`, `abandon` (architect, over tasks);
- `picks` (action selector), `holds` / `wakes` (state_mover).
Prose fields (`why`, `reason`, `note`, `outcome`, `detail`) are words: they name things in words and dates, never a
label or an id. A label token of the current call inside prose is a malformed answer (one correction round, as
today's structural checks).

**New objects a model creates** (an architect's new tasks, a concern door's new concern) get a call-local name in the
answer (`new:1`, or a slug the model chooses), which other fields of the same answer can reference; code mints the
real ids. The architect stops seeing or writing `slug--abc123`.

**Code-written text.** Journals, endings, rollups and reports stop writing ids into sentences. The journal becomes
records (kind, time, related objects as relations, words); a projection renders them. Concern evidence points at its
object (relation) instead of storing a snapshot of a report with ids in it.

**Dates.** Times are data on the object (`when` as the model wrote it + `decided_at`, `day`, `ended_at`), rendered
by the projection. The objective stops carrying them baked in (supersedes e2c260f2's objective string).

**Tools (owner, 2026-10-08: "no tools don't take short ids, this would be way more massive refactor").** Tools
keep taking real ids (pod_fetch, work_graph_peek, read_work_object). A worker's projection shows the id of an object
it may pass to a tool, as a tool argument it can use ("[P2] School notice email ... (pod_fetch id:
datapod:email:...)"); deciders that call no tools (the brain's agents, the architect, state_mover, action_selector,
the finalizer) see labels only. Ids still never go into prose: a worker's findings and a finalizer's outcome name
things in words and put the objects they rely on in `relies_on`.

## 3. Object types (initial list, from the inventories)

| Object | id (code) | data fields (words, times) | relations |
|---|---|---|---|
| Concern | uuid | title, subject, kind, severity, horizon, done_when, owner words, status, first_observed | evidence (message, email/pod, event, work), attached work, list entries |
| Journal record | id | kind, at, words | about concern; refers to work/event/entry |
| Brief | concern id + written_at | what, why_it_matters, tried, owner_wishes, recommendation, open questions; known facts (fact text) | each fact relies_on objects |
| List entry | id | kind, day, what, assistant_part, when, decision, why, status, decided_at | for placement/concern; covers entries; relies_on objects |
| Calendar placement | belief + day | times, statement | belief; related beliefs |
| Belief | key | statement, ... | related beliefs |
| Work object | id | objective, success, status, created/ended at, end reason (words) | made_from entry; for concerns/beliefs; ended_by/duplicate_of/superseded_by work |
| Task (node) | id | title, directive (words), status, wake, finalizer judgment (outcome words, verdict for dayflow only) | depends_on tasks; relies_on (pods, earlier tasks' outputs); ticket |
| Brain event | id | source, occurred_at, text | from message/email/work/presence/clock; concerns routed to |
| Brain decision (matter) | id | per event: decision, reason (words); notes | about events; about/attach for concerns; new concerns |
| Message | id | speaker, at, text, room | |
| Email / pod / finding / frame | pod id | kind, title, body, at | |
| Reminder | id | text, scheduled_at, sent_at | |
| Ticket | id | question, choices, reply text, replied_at | asks for task |
| Calendar event | id | title, start, end, status | |
| Presence spell | id | away/back, from, to | |

## 4. Order of work (each step shippable, tests first)

1. The core: `Projection` (label, render macros per kind, resolve) and the relation store; brain-list entries and
   the day's tasks on it (`relies_on`, `covers` as relations; `why` words; entries rendered with what they rely on,
   dated). This is where today's bugs bit.
2. Brain step and gate: `about` / `attach` / `relies_on` replace the double-duty `events` citation; notes and
   reasons in words; journal records instead of id sentences.
3. The architect: tasks by label (T#) and call-local names for new ones; `ended_by` / `duplicate_of` /
   `superseded_by` relations to other work; directives reference earlier outputs and pods by `relies_on`, rendered
   to the worker as labelled objects; objective and dates as data.
4. state_mover, action_selector, intake_triage: labels in, labels out.
5. Finalizer and worker: outcome and findings in words, `relies_on` for outputs and pods; tools unchanged (ids).
6. Brief writer, concern door, concern planner, stories, noticer, routine writer, belief work_outcome, ticket
   planner, daily context tracker.

## 5. Decisions for the owner

1. One relation store for every link (recommended: one shape, one query path), or relation fields kept per object?
2. Stored history (today's ~1,000 prose fields with labels or ids): leave as history and render it as-is, or render
   it marked as old text? No backfill rewriting is proposed.
3. (Decided 2026-10-08: tools keep ids; workers see the ids they pass to tools.)
4. The noticer copies raw ids and miscopies them: rebuild on the model, or retire it (the brain covers most of it)?
