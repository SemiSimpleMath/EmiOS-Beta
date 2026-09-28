# Belief intake redesign — atomic beliefs with provenance

**Status:** proposal, 2026-09-26. No code written. Needs the owner's review, and explicit approval
for the schema change in §6.

Companion documents: [BELIEF_ENGINE_WALKTHROUGH.md](../architecture/BELIEF_ENGINE_WALKTHROUGH.md)
(how the engine works today) and [belief_engine_defects_2026-09-26.md](belief_engine_defects_2026-09-26.md)
(what is wrong with it).

---

## 1. Why

On 2026-09-26 ten reviewers read all 612 active beliefs, and the atomizer harness ran over the 66
beliefs created in September. The findings that shape this design:

- **Beliefs carry instructions nobody gave.** Intake is told to make every statement "agent-ready"
  and to pair each state with a behavioural rule, so a fact like "prefers typing to voice" arrives
  as "prefers typing … default to typed interaction". The invented half is worse than the fact: it
  tells agents to do things that may not be theirs to do.
- **Long beliefs are made by merging.** Beliefs never merged have a median of 32 words; beliefs that
  absorbed 21 or more others have a median of 89 and reach 278.
- **Duplicates come from the same thing said differently at different times.** "I like coffee" in
  January and "I really love a hot cup of joe" in September become two beliefs. Across the store the
  reviewers found repeated beliefs for one topic under two or three keys.
- **One remark becomes a standing rule.** A single night's reply became a "hard boundary"; a short
  quote was wrapped in a paragraph of procedure.
- **Evidence is not always about the belief it supports.** Legacy beliefs carry whole days of
  unrelated observations (defects §1.8), and identical rows were copied across merged beliefs
  (defects §2.7).

## 2. What a belief is

This definition is shared by intake, merge review and the one-off cleanup of the existing store.
It was worked out on the atomizer and is the contract every writer of belief text follows.

1. A belief is a declarative statement of something true about the user or their world: a fact, a
   preference, a habit, a relationship, a situation, or a wish they expressed. It describes; the
   reader decides what to do with it.
2. Every wish, judgment or rule names who holds it: "The user wants fewer stretching reminders."
3. A belief about an event carries the event's date. Dated, it stays true permanently; only its
   relevance fades.
4. One claim per belief — something that can be supported, contradicted or outgrown on its own. A
   trigger with its effects, a preference with its reason, a claim with its own exceptions, and a
   decision with the wish behind it are each one claim.
5. `kind` is chosen by how long the belief stays useful to know, not how long it stays true.
6. Every belief states something its observations actually say.
7. A belief is understood in isolation. A reader who never saw the day it came from knows, from the
   sentence alone, which person, thing and event it means. A detail that only makes sense next to
   the conversation it came from ("the link", "the task") is not a belief.

## 3. Intake today

`CollectEvidenceStep` gathers fourteen days of daily-insight entries, ticket context and weekly
candidates. `UpdateBeliefsStep` pages them oldest first. For each page, `select_for_evidence`
walks the whole catalogue with the `evidence_match` agent to choose existing beliefs that may be
affected, then one `belief_updater` call reads the page and those beliefs and returns create /
update / deprecate / no-change decisions. Code validates the evidence-to-claim relations and
writes through `BeliefStore`.

One model call is doing three jobs: deciding what the evidence says, finding which existing belief
it belongs to, and writing the statement. Each is done less well because of the others.

**Provenance is lost before intake starts.** The daily-insights writer (`daily_timeline_insights`)
reads `timeline_merged.json`, whose entries are identified: chat entries carry a `message_id`,
time and text; ticket entries a `ticket_id`, time, what was asked, what the user did and wrote;
context entries a time range and label. Its output keeps none of that — each insight carries a
`fact_summary` and an `evidence` list of free-text strings — and `collect_evidence` sets
`source_ref` to None for every insight row. By the time a belief is formed, what remains is a
paraphrase, sometimes a quote, and a date. This is also why identical rows cannot be told apart
from independent observations (defects §2.7).

## 4. Proposed intake

**The pipeline, as agreed with the owner 2026-09-26:**

```
DAILY (build and perfect first)
  daily insights (unchanged, user-facing)
    -> create the day's new beliefs                 (step 1: that day's insights + that day's timeline,
                                                      atomic beliefs citing their items)
    -> check which are duplicates of beliefs held    (steps 2-3, the dedup step)
    -> adjust beliefs                                (step 4, code applies the verdicts)

WEEKLY (deferred until daily works very well; §4b)
  weekly pattern candidates, each citing its days
    -> the same dedup step -> the same writer
```

**Each day is processed once.** Today `collect_evidence` re-reads the last fourteen days of
insights every night, so one insight passes through the updater up to fourteen times — a source of
duplicate beliefs and of the identical evidence copies in defects §2.7. In the new pipeline a day's
insights are read once; what earlier days taught is already in the beliefs held, which is what the
dedup step compares against.

**Intake creates; merging adjusts.** The current `belief_updater` both creates and updates in one
call. Here the creating step only writes the day's new beliefs, and everything that changes an
existing belief — adding evidence, linking a refinement, recording a contradiction — happens in the
dedup step-and-apply step. The two must run back to back in the same pass, so a new rephrasing never
sits beside its original as a duplicate.

### Step 1 — Create the day's beliefs from the insights, with provenance from the timeline

**The daily insights are not changed.** They are a page the user reads — the day's summary and
themes (from the daily assessment summary) and "What … Learned Today" (from
`daily_timeline_insights`) — and the owner ruled on 2026-09-26 that it must not be disturbed: no
change to the insight writer, its prompt, or its output. A prototype that made the insight writer
atomic with citations (`daily_timeline_insights_atomic`) is shelved for that reason.

So the creating step is where atomizing, filtering and provenance happen. For one day it reads two
files and nothing else: that day's `resource_daily_insights.json` (what was noticed) and that day's
`timeline_merged.json` (what actually happened — the user's chat messages, every ticket with what
was asked and what the user did or typed, and the day's context). Code numbers the timeline items;
the creating agent writes atomic beliefs that meet §2, each citing the items it rests on, marked
**said** or **did** (§4a); code restores the exact item — message id or ticket id, time, words.

The insight's wording is a guide to what was noticed; the cited items are the source of truth.
Tested 2026-09-26 (prototype `belief_engine::belief_extractor`): on 09-25 the insight card said
"prefers to retain personal control over … accounts, schools, purchases, forms"; the creating step
wrote four dated decisions, each citing the reply it came from. The card on the page is unchanged.

Readers of `resource_daily_insights.json` (checked 2026-09-26):

| reader | what it uses |
|---|---|
| belief engine, `collect_evidence` | `fact_summary`, `evidence`, `tags`, `temporal_scope` |
| insights page, `routes/insights.py` → `static/js/insights.js` ("What … Learned Today") | `fact_summary`, `tags`, `temporal_scope`, `change_recommended`, `evidence` |
| daily assessment, `archive_daily_assessment.py` | embeds the whole file in `resource_daily_assessment.json`, which the `daily_assessment_summary` agent reads; weekly insights reads those summaries, not the insights file |
| debug status page | lists the latest copy in `daily_insights_pipeline_outputs/` |

No agent loads it as a context item. `extracted_claim` (including `llm_interpretation`) has no
reader at all — it was the ingestion seam for the retired v2 engine. `change_recommended` is read
only by the insights page, where the user sees it.

So adding a citations field is safe for every reader; the assessment agent's input grows by the
cited items, and the insights page can render them. Removing `extracted_claim` affects nothing;
removing `change_recommended` changes what the user sees on the insights page.

### Step 2 — Find the existing beliefs it might belong to

The dedup step in step 3 can only recognise "I like coffee" and "hot cup of joe" as the same belief if
the coffee belief is put in front of it. So each atom needs the right candidates.

Existing practice holds that a deterministic step may rank but must not drop
(`select_for_evidence` deliberately has no top-k embedding gate). Short atoms make it affordable to
keep that rule: at roughly 15 words each, the whole active catalogue fits in one or two pages. The
catalogue is ordered by meaning — `beliefs_for_context` already computes embedding similarity —
so the closest beliefs come first, and nothing is cut.

### Step 3 — Check the atom for duplicates against the candidates

A dedup step agent sees the new atom with its provenance and each relevant candidate with its full
statement and provenance: when each was said, and the words. It returns one of:

| verdict | meaning | what code does |
|---|---|---|
| **same** | the same belief, said again | attach the observation as supporting evidence to the existing belief; create nothing |
| **refines** | the same belief with extra detail | create the atom as a **child** of the existing belief (§5) |
| **contradicts** | says the existing belief is no longer or never was true | attach as contradicting evidence and route the belief to re-evaluation (§7) |
| **new** | nothing existing covers it | create it |

The judgment is the model's, made with full context. No wording or quote matching decides it.

### Step 4 — Code applies

Unchanged in spirit from today: the model never writes. Code validates identities and relations
and writes belief, evidence and links in one transaction.

### 4a. What the user said versus what the user did

The two kinds of evidence carry different weight, and the difference is recorded on each evidence
row rather than inferred later.

- **Said.** A statement in the user's own words — a chat message, a ticket reply. Strong, and able
  to stand alone: "I'm allergic to raw carrots" is a belief on the day it is said. A direct
  correction ("X is wrong, it should be Y") is the strongest case (§7).
- **Did.** Observed behaviour — snoozing, completing, activity at a time of day. A single event is
  not a belief: the timeline already records it. A single evening's behaviour becoming a standing
  rule is how one moment turned into a permanent rule in the current store.

**Patterns are noticed by the insights, not by the belief engine.** Daily insights notice what
repeats within a day; weekly insights notice what repeats across days. A behaviour belief comes
from an insight that identified a pattern, and holds only as far as the items it cites show the
pattern repeating. Cross-day patterns come through the weekly route (§4b, deferred).

Tested 2026-09-26 on five behaviour days: extracting single events produced log entries ("was AFK
starting at 22:20", "did not respond to the CPAP reminder") that never accumulate, because the
dedup step correctly treats two different evenings as two events. Nothing in intake proposes the
pattern, which is why pattern-finding stays with the insights.

### 4a-i. Weight of evidence — later and said outweigh earlier and did

Owner's rule, 2026-09-27: later evidence generally weighs more than earlier evidence, and a
statement in the user's own words weighs more than an inference or a button press. So when a
belief is restated, the most recent thing the user *said* sets the current state, earlier evidence
becomes its dated history, and behaviour-only signals qualify rather than override. The same
ordering applies to the strength calculation once the new store feeds it.

### 4a-ii. How a belief evolves — the dog-walk case

Owner's account of the old system at its best: a belief "the owner takes the dogs out at 8am" formed
from the calendar. Prompts at 8am were answered "no, the spouse took them"; after a few of those the
belief became "sometimes the owner takes the dogs out at 8, sometimes the spouse". The new pipeline must keep
that behaviour.

- **The creating step keeps dated events.** "On 2026-02-12 at 08:00, the spouse took the dogs out" is
  useless on its own and valuable the moment it bears on a held belief. The creating step never sees
  the held beliefs, so it cannot tell which case it is; it records the day's events as dated facts,
  never as a habit (tested 2026-09-26: an earlier "is it useful later?" filter in the creating step
  dropped exactly these events and would have killed the dog-walk evolution).
- **The dedup step connects the event to the belief it bears on.** Against "the owner takes the dogs out
  at 8am", the event is `contradicts`, and it is attached as contradicting evidence. An event that
  bears on nothing held becomes a short-lived dated belief that fades within days — and is still
  there to be matched if the same thing happens again.
- **A revision step rewrites the belief once its evidence disagrees with its wording.** When
  contradicting evidence accumulates on a belief, the belief goes to re-evaluation, which reads its
  whole dated evidence and rewrites the statement — "sometimes the owner, sometimes the spouse". The existing
  `belief_reevaluator` already does this for contested beliefs; the new intake has to route
  contradicted beliefs to it. NOT BUILT in the replay yet: today contradicting evidence lands on the
  belief and nothing rewrites it.

### 4b. The weekly route — DEFERRED

**Deferred by the owner until the daily route works very well.** Recorded here so it is not lost.

Some beliefs only exist across days: "the user has slept badly all week", "the user codes past
23:00 most weeknights". No single day contains them, and per-day intake plus the dedup step will not
produce them — the dedup step rightly sees "slept badly Monday" and "slept badly Tuesday" as two nights.
This was the one legitimate thing the old fourteen-day window did.

Weekly insights already produce these. They read the week's seven daily assessment summaries (the
day's headlines, dominant themes, ticket and work summaries) and write `belief_candidates`, e.g.
"hydration and movement needs frequently become overdue in late evening … observed on 5+ days each
week". Today those enter the belief engine at weight zero as "interpretations", which is why
pattern beliefs look evidence-free in the current store.

The weekly route, when built:

1. Each weekly candidate cites the days, and through them the items, it rests on.
2. It is written as a belief by the same creating step and checked by the same dedup step.
3. It counts as evidence, not weight zero — it is where behaviour patterns legitimately come from.
4. A pattern that recurs the next week is `same` and gains another week of evidence; a bounded
   period ("bad sleep this past week") is episodic and loses relevance within days.

## 5. Parent and child beliefs

A refinement is kept as its own belief, linked to the belief it refines:

```
The user likes coffee                     ← Jan 22: "I like coffee"
  └─ The user likes their coffee hot      ← Sep 3: "you know I really love hot cup of joe"
```

- **One level only.** A refinement of a refinement attaches to the same parent. This keeps it a
  grouping, not a taxonomy.
- **Evidence rolls up.** A child's evidence also supports its parent (liking hot coffee is liking
  coffee). A parent's evidence says nothing about the child.
- **Each keeps its own life.** A child can be contradicted, decay or be dropped without touching
  the parent.
- **Retrieved together.** Whoever retrieves a parent receives its children with it, so a reader
  sees "likes ice cream" together with "not on Sundays" and "not mint" — the connection that
  atomizing would otherwise lose.

This also gives the merge matcher's `specialises` verdict a destination. Today it writes a receipt
and does nothing; 105 pairs sit in that state.

## 6. Schema change — needs approval

`user_beliefs` has no parent link. The proposal is one nullable column, `parent_belief_id`,
referencing `user_beliefs.id`, with the one-level rule enforced by code (a parent may not itself
have a parent). A link table is the alternative if more than one parent is ever wanted; the
one-level design does not need it.

Archiving a parent must keep its children reachable. That rule belongs with the change.

## 7. Contradictions and direct corrections

"Contradicts" attaches contradicting evidence and queues the belief for the re-evaluator, which
already reads a belief's full history. Today a single user comment cannot flip a belief to
contested (defects §4.1). The owner has said a direct statement such as "X is wrong, it should be
Y" is extremely strong. How strong — immediate re-evaluation, or treated as authoritative on its
own — is an open decision (§11).

## 8. Merge review follows the same definition

Merge review's `same` verdict currently writes a combined statement, which is how long beliefs
formed. Under this design a `same` keeps one belief's statement (or rewrites it to §2) and makes
the other's evidence reachable, never a concatenation. `specialises` becomes a parent/child link.
With atoms on both sides, most comparisons become a clean same-or-not.

## 9. What this retires

| defect | how |
|---|---|
| invented directives in beliefs | §2 rule 1–2 at every writer |
| long beliefs from merges | §8 |
| duplicates from rephrasing | step 3 `same` with full context |
| one remark turned into a standing rule | step 1 writes what the observation says, with its date; behaviour needs repetition (§4a) |
| insight provenance lost (`source_ref` None) | step 1 cites the day's timeline items |
| identical copies counted as independent observations | same cited item = same observation |
| `specialises` dead end | §5 |
| a belief with no evidence | step 1 only produces atoms that cite an observation |

Legacy evidence damage (defects §1.8, §2.7) is not retired by intake; it is handled by the one-off
cleanup of the existing store.

## 10. How to test it before switching

- **Replay, read-only.** Run the new steps over the last fourteen days of daily-insight entries
  against a copy of the database. Compare with what the current updater produced: beliefs created,
  duplicates, beliefs containing instructions, beliefs without evidence.
- **Variance.** Each step on the same input three times; the verdicts should agree.
- **Deliberate rephrasings.** A small fixed set of pairs said differently at different times
  ("like coffee" / "hot cup of joe") that must come out `same` or `refines`, and pairs that look
  alike but are different beliefs that must come out `new`.
- **Behaviour.** A single day's behaviour must not create a standing belief; the same behaviour on
  several dated days must accumulate under one belief.
- **Regression set.** Every case learned in testing — separate replies not merged into one broad
  rule, a detail that only makes sense in context, a rule proposed from one evening, a rule the user
  never stated, a stated allergy — re-run on every prompt change.

The atomizer harness (`app/assistant/tests/agent_tests/belief_atomizer/run_atomizer.py`) is the
pattern: read-only database connection, output to `scratch/`.

## 10a. Results — full 185-day replay, 2026-09-27

Pipeline as built (extract → dedup → revise, `belief_engine/intake/`), replayed over every stored
day 2026-02-10..09-25 into `scratch/belief_replay/`. 48 minutes, ~$12 (41.6M input tokens, 99% in
dedup). 513 beliefs + 37 refinements, 1,348 evidence rows, 17 contradictions → 17 revisions.
Scored by five reviewers against the LIVE store (612 active, with the earlier keep/drop/unsure
verdicts attached).

**Coverage of the live store:** 528 of 612 covered. Of the reviewer's 360 keeps, 333 covered; of the
48 drops absent from the rebuild, 48 absent for the right reason. ~18 distinct real losses:
(a) beliefs whose only source is the subconscious **feedback extractor** (`user_comment`: meal
preferences, the spouse's love language) — a source the new pipeline does not read; (b) concrete
payload abstracted out of the statement into the evidence (email addresses, the spouse's birthday, the
salmon method, an 8-rating threshold); (c) calendar-derived blocks (Work Hours 09:00–16:30) that
never appear in chat; (d) nudge-response patterns (weekly route, deferred).

**Quality of the rebuilt beliefs (550):** better than live 104, same 293, worse 47, new and useful
32, noise 74 (13%; 4% on the first 40 days — later months carry more one-off task chatter).

**Defects to fix before the rebuilt store replaces live:**
1. Misreadings (3): "our montheversary" resolved to the assistant instead of the spouse (B193); "my
   favorite is poached" (the restaurant Poached Kitchen) read as poached food (B106/B161); a
   bedtime goal invented from "walk to bed" (B3).
   **Fixed 2026-09-27 (B193, B106) — the cause was input, not prompt.** The stored timeline holds
   only the user's turns and the extractor knew only the first name. `day_items` now adds the
   household roster (`resource_user_data.important_people` + `additional_context`) to the system
   prompt and the assistant's conversational turns (`unified_log_2026`, master_room, role
   assistant, sources room_ui/chat/slack — reminders, subconscious digests and room summaries are
   excluded; JSON-rendered tool reports show as their one-line feed) to the timeline as
   `assistant reply` items. First attempt at that query compared an ISO `T` bound against the
   log's `YYYY-MM-DD HH:MM:SS.ffffff` strings and so returned the *next* day's replies, which is
   what produced the 09-25 "personal control" regression in the earlier trial; with the bounds
   formatted like the column, montheversary → "the owner and the spouse" (2/2) and Poached → the restaurant
   (2/2). B3 is different: the 10:30 comes from the insight writer's evidence note quoting the
   assistant's *prompt* ("your 10:30 PM screen-free goal"; the ticket was accepted, so it is not
   a timeline item). Describing those notes to the model as "quoting the assistant's prompts"
   made it worse (4/4); the rule "the times, numbers and names in a belief are those in the items
   it cites" brought it to 1/4 but cost the regression set 3 cases (15/16 → 12/16: a real atom
   dropped on 09-24, 09-25 fused again), so it was reverted. **B3 stays open** with the plain
   prompt (1/2). Remaining option: pass accepted tickets as context items (owner ruled them out
   as *signal*; as context they would show the number is the assistant's) — needs the owner's
   call. Regression with the plain prompt and the corrected inputs: 15/16 (the one failure is the
   09-25 fusion, which flips trial to trial on identical input).
2. Payload retention: when the statement summarizes, the concrete detail in its own evidence is
   lost (~10 cases).
   **Measured 2026-09-27, left OPEN.** One prompt sentence — "An address, a date or a name the owner
   gave is kept exactly, in the statement it belongs to" — took the payload cases (08-18 the spouse's
   birthday, 06-13 Pieter's address) from 1/4 to 4/4 with the original eight regression cases
   unchanged (15/16 before and after). But it reopened the log-entry class: a fresh 40-day replay
   (v6, `scratch/belief_replay_40d_v6`) had episodic beliefs 8 → 13 with 03-08 emitting "On
   2026-03-08, the owner deferred or declined getting coffee for himself"; isolated on 03-08 ×3, the
   rule produced that four-entry log 1/3 and the bare prompt 0/3; a first wording with "a number"
   also brought clock times back (08-31). ~10 payload cases over 185 days against noise on many
   days is the wrong trade, so the rule is withdrawn and the bare prompt stands. Fix, if wanted,
   belongs in a step that can see the evidence a statement summarized, not in extraction.
   **Structural limit, not fixable here:** a detail
   no insight noticed never reaches the extractor, because beliefs cite insights. 06-07's
   addresses (given in a ticket reply; the writer noted only the sign-off) are that case; so is
   class (b) above where the writer summarized. The daily insights writer is off limits, so
   those details are lost unless a second source (e.g. the ticket replies themselves) is ever
   added as intake.
3. Revision does not propagate: after B238 dated the end of intermittent fasting, five sibling
   beliefs still assert it as current; same for the son's grade and the Nest token. The
   held-beliefs-contradict sweep (§7 case 2) is now measured, not hypothetical.
   **Fixed 2026-09-27 — `intake/propagate.py`, wired into the replay.** Dedup names one target;
   on `contradicts` the same atom is now judged again against the store minus that target, and
   again for each further `contradicts` (capped at 5 rounds), each hit revised like the first.
   Harness `agent_tests/belief_dedup/run_propagation.py` rebuilds the store as held before a day
   and runs the fan-out read-only. Fasting case: 2/2 trials catch exactly B1 and B291 and leave
   B76 (IF improved reflux — historical) and B156 (dislikes IF) alone. Sweep of all 17 recorded
   contradictions: 9 further beliefs caught — 7 right (fasting ×2, stale lights-off times ×4
   across April and July, the son's grade), 1 marginal (B400 AC tune-up window), 1 spurious
   (B157, a chat-memory belief whose *evidence* carried a lights time from a whole-message
   `same` attachment — §2.7 in the defects doc, not a fan-out fault). The July lights case also
   shows defect 4: the revision of B185 became a three-date changelog.
4. "Changelog" statements: some revisions stuff every dated contradiction into one sentence
   (B90, B150, B185) instead of stating the current belief and citing the change.
   **Fixed 2026-09-27 — `belief_revise` form + prompt.** Two causes: the prompt's own "states
   what holds now and, dated, what held before" was read as *always append the history* (baseline
   re-run of the 17 recorded revisions: 10 diaries, 17/17 with a dated tail), and the form had no
   way to say the belief stands, so a confirmation (B510) or a spurious contradiction (B91) was
   rewritten anyway. Now: `outcome: revised | unchanged` (code keeps the held statement on
   `unchanged`; replay and fan-out skip the write), the statement is "the belief as it stands, in
   the present; when the state changed, one dated clause says what held before and until when;
   the evidence keeps the history". The first wording of the standing rule ("a single later
   occasion leaves the belief as held") over-corrected — 6 of 8 `unchanged` were wrong, including
   "its about 5pm not 6pm" and "the son is a junior" in September — fixed by the distinction *what
   the owner says about the state itself is the new state even said once; what he reports about one
   occasion is that occasion*. Result over all 24 recorded revisions (185-day store + v6), read one
   by one: 21 right, 2 defensible `unchanged` (B202 different claim, B352 one aborted attempt),
   1 wrong revision (B91, driven by dedup's spurious contradiction — defect 5). Diaries 10 → 2
   mild. Harness: `agent_tests/belief_revise/run_case.py [--all] [--store DIR]` counts DIARY /
   HEDGE / unchanged. The first-exception-unchanged path is also the dog-walk evolution: the
   contradict evidence is attached on the first differing occasion, and the second one revises
   from both.
5. Spurious contradictions: dedup marked a June outage remark as contradicting CPAP use (B91) and
   a confirming Sept 11 reply as contradicting (B287, B510).
   **Fixed 2026-09-27 (with §2.7) — `belief_dedup` prompt + form, `replay_store.apply`.** Reading
   all 48 `same`/`contradicts` decisions showed two faults. (A) Dedup matched a candidate's
   *evidence*, not its *statement*: "the owner's birthday is March 18" and "the spouse's birthday is August
   18" were filed `same` under B94 ("the owner regards the assistant as a friend…") because B94's evidence holds
   a pasted task note with both birthdays — so the spouse's birthday never became a belief, which is the
   payload-loss mechanism the extractor could not fix. B510's `contradicts` was against a ticket
   prompt in its evidence; B91 and B202 the same shape. (B) `same`/`contradicts` attached *every*
   source of the atom to the target (B20 gained "pretty tired, just took out the dogs"; B28 two
   deferred tickets) — §2.7, and the pollution feeds A on later days. Fix A: "A belief claims what
   its statement says; its sources show what that claim rests on and settle what the words meant.
   Decide between the claims." Fix B: the form gained `sources: [n]` — dedup names which of the
   new belief's (numbered) sources bear on the target; `apply` attaches only those; refines/new
   keep all. Harness `agent_tests/belief_dedup/run_decisions.py` re-judges recorded decisions
   against the store as held that day. Baseline: 44/48 calls reproduced, B94 ×2 and B91
   deterministic. After: B94 ×2, B510, B202, B352 → `new`; B91 remains (1 of 5). Three real
   contradictions became `new`/`refines` (B185, B381, B375-June) — duplicates, the safe side of
   the false-merge rule; B219 → `refines` carrying Pieter's address is payload retention by the
   right door. Bearing sources read right in every case checked (B28 drops "family time will
   start late today", B314 drops the question). Consequence: on `contradicts` the atom's
   non-bearing sources are attached nowhere (before: polluted the target). Cheap for single-claim
   atoms; the upstream rule stays one claim per atom.
6. One-moment task states minted as beliefs ("a Porto's order was placed, expected Friday"), and
   loose kinds (a one-off command as stable_preference; a chronic condition as episodic).
7. Evidence text is verbatim: a pasted password (B237) is in the store. (The earlier note of a
   Stripe key in B428/B429 was wrong: those hold the sentence "i gave it the full stripe api
   key", no key.) **Fixed 2026-09-27 — `intake/redact.py`**, applied in `extract_day` to evidence
   text, the prompt answered and the statement: credential values after password/passcode/pin/
   api key/token/secret with an explicit "is"/":"/"=", and keys recognisable by prefix
   (sk_live, sk-, AKIA, ghp_, xoxb-, AIza). Unit tests
   `non_agent_tests/test_belief_intake_redact.py` (8, invented data). Read-only scan of the whole
   185-day store: 3,246 texts, 3 changed, all genuine — the HBO password, a Zoom passcode in a
   prompt, Marika's invite token (B549, the "private key") — zero false positives.

**End-to-end check, 2026-09-27 evening — 40-day replay v7** (`scratch/belief_replay_40d_v7`,
fan-out + redaction + outcome-aware revise + bearing-source dedup): 158 beliefs + 5 refinements,
432 evidence rows; verdicts same 3 / refines 5 / contradicts 3 / new 158; 3 revisions, each "X;
until DATE, Y"; episodic one-offs 9 (v6 with the withdrawn payload rule: 13; v5: 8). No fan-out
cascades in 40 days (expected — siblings are a months-later effect; the 17-contradiction sweep on
the full store is that proof). Nothing left in §10a that blocks shadow mode except the owner's
decisions below.

## 10b. Shadow mode — running from 2026-09-27

Owner's decisions (2026-09-27 evening, "go with your recs on everything"): the one-level parent
link is approved (`parent_id` on the intake's own beliefs table; the `user_beliefs` column is a
cutover matter); direct corrections stay on the ordinary evidence path until shadow mode shows a
refusal; accepted tickets stay out; the 360 kept legacy beliefs are fed through the intake after a
week of shadow mode, not written directly; shadow mode starts now.

Shape: `belief_engine/intake/store.py` (`IntakeStore`, one schema, prefixed `belief_intake_` in
the app DB, bare in a scratch file; app-DB writes through db_manager, one short transaction per
apply/revise/mark, never across a model call), `intake/run.py` (`judge_day`/`run_day`/
`run_pending`, shared by the replay and the routine), `intake/routine_adapter.py`
(`BeliefIntakeAdapter`, pipeline id `belief_intake`, registered in `pipeline_registry`), routine
`configs/routines/public/belief_intake.json` daily 01:00 — after daily_insights (00:05), beside
the old belief_engine (00:30). It processes the boundary day (yesterday) plus any unfinished
earlier day, raises if the day's files are missing, and writes nothing any reader uses. Day window
is the pipeline's (05:00→05:00 local; the assistant-turn query was corrected to it). Backfill of the
185 stored days into the app tables ran 2026-09-27 evening by hand (`python -m
belief_engine.intake.run --through 2026-09-26`): 185/185 days done in 62 min, 501 beliefs + 32
refinements, 1,352 evidence rows (5 redacted), verdicts new 501 / same 37 / refines 32 /
contradicts 30, 23 revisions all in "X; until DATE, Y" shape, fan-out live (05-07 fasting cascade
revised B1, B284, B78 beside the named B35; the chat-memory belief came back `unchanged`).
Live `user_beliefs` active at the same moment: 612. Tests: `non_agent_tests/test_belief_intake_store.py`.
Cutover (readers → new tables, retire collect_evidence/belief_updater) is a separate change after
the owner has compared `belief_intake_beliefs` with `user_beliefs`.

## 10c. Cutover plan — owner wants to switch as soon as shadow mode holds up (2026-09-27)

Consumer map (full sweep of app/, configs/, scripts/; every reader and writer with fields):
almost everything reads the EXPORT `resources/kg_derived/resource_user_beliefs.json`, not the
tables — dayflow_routine_stage (belief_key must be unique; `[belief:key]` citations; short_id,
confidence, kind, domain, conditions, first/last dates), entertainment_advisor_stage (tags ∩
pull_set), health_status_stage (domain ∈ {health, sleep} or tags; sorted by confidence),
feedback_extractor context (domain filter, observation_count), insights UI. One production reader
of `beliefs_for_context`: the meal lane (query + `meal_engine` tags; statement, last_confirmed,
kind, observation_count). Raw-SQL readers: `/beliefs` admin routes, `/api/personalize` (orphaned).
Writers outside the engine: feedback_extractor_persist (`user_comment` evidence → upsert by
belief_key, the meal lane's main source), work_feedback (resolves `belief_key` from work-object
`belief_refs`; deprecate/revise), `/api/beliefs/update` (manual corrections, lock), and the
belief_archive / belief_tag_v1 routines. `belief_key` is a durable cross-system identity.

Order, each step reversible by pointing back at the old store:
1. **Export from the new store** (`export_beliefs` reads `belief_intake_*`): belief_key = short_id
   = the B-id (stable, never reused); statement, kind, scope; status active; observation_count,
   first_observed, last_confirmed derived from support evidence; children exported as beliefs;
   confidence omitted (consumers that sort by it sort by observation_count). Switches dayflow
   routine writer, health, entertainment, feedback-extractor context, insights UI in one move.
2. **Tags on the new beliefs**: point `tagging.tag_beliefs` (belief_tag_v1 routine) at the new
   table; `belief_tags.belief_id` holds B-ids. Until it has run, retrieval and the tag-filtered
   stages fall back to the whole catalog by design. Domain filters (health, feedback context)
   become tag filters — two small consumer edits.
3. **`beliefs_for_context` from the new store** — but only after step 5, because the meal lane's
   beliefs come mostly from `user_comment` evidence the intake does not read yet.
4. **Work feedback**: `get_by_key` resolves B-ids; outcomes attach as evidence
   (`source_ref = work:<id>`) and go through revise; `resolve` needs a `status` column
   (active | retired) on the new beliefs table — add before this step.
5. **Feedback-extractor comments as an intake source**: the daily comment pods become timeline
   items ("said", `source_ref = pod:<id>`) so the extractor/dedup/revise path handles them like
   chat. Replaces feedback_extractor_persist's own upsert. Closes the §10a coverage gap.
6. **`/beliefs` admin UI** on the new tables (list/item/update/trends); manual correction = a
   revision with `source_ref = owner`, lock = a flag the reviser honours.
7. **Retire**: belief_engine routine (collect_evidence/update_beliefs/reevaluate/canonicalize/
   decay/snapshot), belief_archive, the v1 chroma collection; drop nothing in the DB until the
   owner says so (backup exists).
Losses to accept at cutover: decay/confidence bands (the new store has none — beliefs are a
view of evidence), the legacy 612's evidence history (the 360 keeps come back through the intake
in step 5's week), and nothing else identified.

## 10d. Beliefs in chat — the new store's first reader (2026-09-27)

Owner's choices: k = 10 with a surfacing log, narrow later from data; dates shown; no rule
about mentioning beliefs — the assistant acts on them naturally. Built: `belief_engine/intake/recall.py`
(`recall` → `rank`, a pure pick-from-candidates step so a per-candidate classifier can replace
the vector cutoff later; `format_for_prompt`; `log_surfaced` → `belief_intake_surfaced`), the
`relevant_beliefs` context item in `context_injector` (master_room only — beliefs describe the
owner and other rooms have other participants; memoized per inbound message; degrades to empty
with an ERROR log like the other optional context), `agents/master_room/chat_gate` `config.yaml` +
`user.j2` section (the master room has its own gate; the first edit went to `room::chat_gate`,
which never serves the master room — the restart showed a prompt without the section) with the title
"WHAT I KNOW ABOUT JUKKA (may or may not be relevant)" with one rule: a belief shapes the reply
when it changes what a good reply is; when the latest message says otherwise, the message wins.
Two lanes: closeness in meaning to the message and the people it names (+0.15 for a belief naming
one of `important_people` present in the message). Weights had to change from retrieval.py's
0.55/0.25/0.20 to 1.0/0.06/0.04: over the whole catalog, recency let "annual physical" (seen
three days ago) outrank the AC-service belief for an AC-service message. Relevance floor 0.30 on
cosine (measured: the beliefs a message is about sit at 0.37–0.86; 0.24–0.30 is topic-adjacent
noise), so k is a ceiling. Harness `agent_tests/belief_recall/run_recall.py "<message>"` (read-only).
Unit tests `non_agent_tests/test_belief_intake_recall.py`. Needs an the assistant restart to load (the YAML
change alone resolves to empty on the old code, harmlessly). The loop closes through the intake:
chat is its main source, so a reply that contradicts a surfaced belief is the next night's
contradict evidence.

## 11. Open decisions for the owner

1. Approve the `parent_belief_id` column (§6).
2. How strong a direct user correction is (§7).
3. RESOLVED 2026-09-26: the daily-insights writer and page are not changed. Provenance comes from
   the creating step reading the day's timeline (step 1).
4. Whether ticket replies become weighted observations again (defects §1.3). With step 1, a ticket
   reply reaches the belief engine as a cited "said" item through its insight; this may settle it.
5. RESOLVED 2026-09-26: atomizing and filtering happen in the creating step; insights stay as they
   are.
6. The existing store: the ten-reviewer pass produced `scratch/belief_review_2026-09-26.csv`
   (keep / drop / unsure with clean statements). How the kept statements are loaded — through the
   new intake as if newly observed, or written directly — is decided after intake exists.

## 12. Not in scope

Retrieval into agents (the daily compile, the chat experiment), decay tuning, and the legacy code
removal listed in the defects document.
