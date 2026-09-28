# Belief Engine — a plain-English walkthrough

This document explains, in order, what happens to a piece of information on its way to becoming
a belief the assistant acts on. It is written to be read start to finish rather than searched.

It is a **companion** to [16_BELIEF_ENGINE.md](16_BELIEF_ENGINE.md), which is the reference:
tables, columns, file paths, configuration keys, and the live-versus-dead code map. Where this
document says "the code checks X", the reference tells you which file and line. Nothing here
contradicts it; this is the same system told as a story.

Known defects are deliberately **not** in this document. It describes how the system is meant to
work and how it does work. The problems are listed separately.

---

## 1. The shape of the thing

Once a night, at 00:30 local time, a single job runs. It looks at roughly the last two weeks of
recorded observations, decides what they say about you, and updates a set of statements called
**beliefs**. Nothing else in the day writes beliefs, with two small exceptions described in
section 10.

The job has five steps, always in this order:

1. **Collect** the evidence worth considering.
2. **Update** — decide what beliefs that evidence creates or changes.
3. **Recompute** how strong every belief currently is.
4. **Re-examine** any belief that is in dispute.
5. **Merge** beliefs that turn out to be saying the same thing.

Each step hands its work to the next through a shared run object. If a step fails, the run stops
there and the remaining steps do not happen that night.

A note on vocabulary before we start. Several words in this system have specific meanings:

- A **belief** is a single sentence about you, stored as a row, with a stable name.
- **Evidence** is a dated record of something that happened, attached to a belief, which is the
  reason that belief exists.
- A **domain** is the one area a belief is filed under — routine, health, food, work, and so on.
  It is a filing label, not a boundary; the engine does not run separately per domain.
- **Tags** are an additional, additive set of labels used for finding beliefs later. A belief has
  one domain but can have many tags.
- **Valence** is the direction a piece of evidence points: does it *support* the belief,
  *contradict* it, or *qualify* it (narrow it, add a condition).

---

## 2. What a belief actually is

A belief is a row with a handful of fields that matter:

- A **key** — a stable dotted name like `routine.lights.daily_off_time_10am`. This is the belief's
  identity. It is unique: no two live beliefs share one.
- A **statement** — the sentence itself, in plain language, written to be read by an assistant.
- A **confidence** of high, medium or low.
- A **scope** of either *chronic* (an ongoing thing) or *temporary* (something bounded in time).
- A **kind**, which is the important one for how the belief ages. The kinds are: durable fact,
  stable relationship, stable preference, routine pattern, episodic context, and transient state.
  The kind decides how fast the belief loses force, which section 6 covers.
- A **status** of active, contested, or deprecated.
- A set of **computed strength numbers**, recalculated nightly, which section 6 explains.

Separately, each belief has a list of **evidence rows**. Each of those records what was observed,
when it was observed, where it came from, how much it weighs, and which direction it points.

The dates matter more than they look. A piece of evidence carries the date the thing *happened*,
not the date the system got round to reading it. Ageing is computed from the former, deliberately,
so that processing a backlog does not make old observations look fresh.

---

## 3. Step one — collecting the evidence

The first step gathers everything from roughly the last fourteen days that might bear on a belief.
It draws from three places.

**Daily insights.** Every day, a separate part of the system writes a file summarising what it
observed about that day. This step reads those files and keeps the entries whose tags overlap with
the areas the belief engine is configured to care about. Each kept entry becomes one piece of
evidence. An entry marked as being about an ongoing pattern is given a weight of 3.0; anything else
gets 1.5. Weight is how much that observation counts toward the belief's strength. These are the
only evidence items in the nightly path that carry real weight.

**Ticket signals.** Tickets are the pop-up questions and notifications you answer during the day.
This step gathers those, groups them by the kind of ticket, and produces one bundle per kind. Those
bundles are given a weight of zero. They are included so the model reading them has context about
what has been happening, but they are explicitly not counted as independent support for anything.

**Weekly insights.** A weekly summarising job produces candidate observations. These are also given
zero weight, and for the same stated reason: a weekly summary is an *interpretation* of daily
observations that have already been counted. Counting them again would double-count the same day.

That zero-weight rule is a real design decision and worth understanding. The system distinguishes
between things it should *read* and things it should *count*. Context can be read without being
counted.

---

## 4. Step two — forming and updating beliefs

This is where evidence becomes a belief.

**Sorting and batching.** Everything collected is sorted oldest first, so that if something was said
on Monday and corrected on Wednesday, the correction is read second. That list is then cut into
batches small enough to fit in a single request to a language model. If a batch still turns out to
be too large once assembled, the code splits it in half and tries each half, repeating until every
piece fits. A single item too large on its own is never trimmed to make it fit — the run stops and
says so, on the principle that quietly sending half a source is worse than sending none of it.

**Finding which existing beliefs are relevant.** Before asking what changed, the code needs to show
the model which beliefs already exist and might be affected. It cannot show all of them, so it asks
an agent called **evidence_match** to choose. That agent is shown the new evidence and a page of
existing belief summaries, and returns the ones worth considering. Two safeguards apply: the agent
is given short local identifiers rather than real names so it cannot invent a plausible-looking key,
and if it returns something that was not on the list, it gets exactly one correction attempt before
the run fails. The full original records are then restored by ordinary code — the model's output is
used only to *select*, never to reproduce the text.

**Asking what changed.** Each batch, along with those selected beliefs, goes to an agent called
**belief_updater**. It returns a list of decisions, one per belief: create this new one, update that
one, deprecate a third, or leave one unchanged. It also supplies the statement, confidence, scope,
kind, and domain for anything it creates or changes.

Crucially, the updater does not write anything. It proposes; ordinary code decides whether the
proposal is legal and then writes. That separation holds throughout this system.

**The evidence-to-claim rule.** For every piece of evidence the updater cites, it must also supply a
separate entry saying *how* that evidence bears on the claim — support, contradict, or qualify —
along with its reasoning. The code rejects the whole belief if any cited evidence lacks that entry,
if the same item is cited twice, if a cited item was not in the batch, or if the direction is not
one of the three permitted values. The direction is never inferred from whether the underlying
event sounds positive or negative. A grumpy message can support a belief; a cheerful one can
contradict it.

**Writing.** Accepted decisions go to the store, which writes the belief row, its evidence, and its
observation counts in a single transaction — either all of it lands or none of it does. Only after
that commits does the system update the search index used for finding beliefs by meaning.

One useful detail: the counts of how many times something was observed, and the first and last dates
it was seen, are never set directly by the writer. They are recalculated from the evidence rows
themselves after every write. Identical observations are collapsed so that reading the same thing
twice does not look like seeing it twice.

**Tolerating a bad row.** If the model produces one malformed belief among many, that belief is
skipped, counted, and named in an error message, and the rest of the run continues. The step
declares that it tolerated a failure rather than hiding it.

---

## 5. Step three — recomputing how strong each belief is

Nothing about a belief's strength is stored when it is written. It is recalculated from scratch
every night from the evidence, and this step does that.

**Ageing each observation.** Each piece of evidence starts at its recorded weight and loses force
over time on a half-life curve: after one half-life it counts half as much, after two half-lives a
quarter, and so on. The half-life comes from the belief's kind:

| Kind | Half-life |
|---|---|
| Durable fact | never decays |
| Stable relationship | 1825 days (five years) |
| Stable preference | 365 days |
| Routine pattern | 90 days |
| Episodic context | 14 days |
| Transient state | 1 day |

The half-life is frozen onto each evidence row when it is written. Reclassifying a belief later
therefore cannot retroactively rewrite the ageing of observations already recorded — history stays
as it was understood at the time.

**Adding it up.** The aged weights are summed into two separate totals: everything supporting the
belief, and everything contradicting it. They are kept apart rather than netted into one number,
which lets the system tell the difference between "we know little about this" and "we have strong
evidence pointing both ways".

From those two totals it computes a **band** — a plain-language summary of where the belief stands:

- If contradiction makes up 60% or more of the total, the band is *deprecated by contradiction*.
- If support and contradiction are both substantial, the band is *contested*.
- Otherwise the band is *high*, *medium*, *low*, or *faded*, by how much support remains after
  subtracting contradiction.
- A belief with no countable evidence at all gets *unverified*.

**What the band does.** Two bands have consequences. *Faded* marks the belief deprecated, which
starts it on its way out of the active set. *Contested* (and *deprecated by contradiction*) marks
the belief as being in dispute, which is what step four picks up. The other bands are recorded but
do not change anything by themselves.

Beliefs the owner has locked are skipped entirely by this step, as are beliefs that are not active.

---

## 6. Step four — re-examining beliefs in dispute

This step only runs if there is something to do: if no belief is contested, it exits immediately
without spending anything.

For each contested belief, the code assembles the belief and its **complete** evidence history and
hands both to an agent called **belief_reevaluator**. "Complete" is meant literally — the history
follows the belief backwards through any merges in its past and reads the archive tables as well as
the live ones, and it is explicitly not shortened to fit.

The reevaluator can do one of five things: confirm the belief as it stands, rewrite its statement,
qualify it (narrow it, add a condition), split it into more than one belief, or deprecate it. It may
also change the confidence, scope, and status. If it splits a belief and leaves the original with
nothing to stand on, the code deprecates that original automatically rather than leaving an orphan.

Beliefs the owner has locked are filtered out before any model is called, so a locked belief costs
nothing to skip.

---

## 7. Step five — merging beliefs that say the same thing

Over time the engine accumulates beliefs that overlap. This step finds and combines them. It is the
most heavily defended part of the system, because a bad merge destroys information.

**Deciding what to look at.** Every belief has a content fingerprint — a hash of its statement,
conditions, and observations. The step compares each belief's current fingerprint against the one
recorded the last time it was examined. Only beliefs whose fingerprint has changed are considered,
which keeps the nightly cost proportional to what actually moved. There is also a fingerprint of the
*prompts and output formats* of the agents involved: change any of them and every previous judgment
is invalidated and re-examined, because a judgment made under different instructions is not
trustworthy.

**Proposing pairs.** An agent called **match_discover** is shown a belief and a page of candidates
and proposes which pairs might be about the same thing. It only proposes; nothing is merged here.
The same invented-identifier protection used earlier applies, with one correction attempt.

**Judging a pair.** Each proposed pair goes to an agent called **match_review**, which is shown both
beliefs and their complete evidence and returns a relationship:

- **same** — one belief, stated twice. Requires a written replacement statement.
- **supersedes** — one has replaced the other.
- **contradicts** — they genuinely conflict.
- **specialises** — one is a narrower case of the other.
- **different** — unrelated.
- **unresolved** — cannot tell.

**When a pair is too large to read at once.** Some beliefs carry years of evidence — enough that the
pair does not fit in a single request. Rather than trimming, the system shards the records into
fragments and asks an agent called **evidence_page** to produce one finding per fragment, then hands
the findings to the reviewer. The code requires exactly one finding per fragment, with the fragment
identifiers echoed back precisely; if any are missing, invented, or repeated, it re-asks once with
the specific identifiers that were wrong, and refuses the pair if that also fails. If the accumulated
findings still do not fit, the pair is parked in a durable "not reviewed, ran out of budget" state
rather than being decided on partial information.

**A second opinion.** For the two relationships that destroy a belief — *same* and *supersedes* — a
third agent called **merge_check** reviews the proposal. It is explicitly told the first judgment is
a proposal and not authority, and it must confirm that no meaning was changed. Anything short of a
clean approval is downgraded to *unresolved*, and nothing happens.

**Applying it.** Only then does the code write, inside a single transaction that re-checks the
content fingerprints one final time — so a belief edited while the review was running cannot be
merged on the basis of stale text. For a *same* verdict: the surviving belief takes the new
statement, inherits the other's tags, the loser is marked deprecated, and a row is written recording
which belief was absorbed into which. That last row is what lets the surviving belief's history
still reach the absorbed belief's evidence afterwards. A before-and-after snapshot of both is stored.

Evidence is never moved. It stays attached to the belief it was originally recorded against, and is
reached through the merge record.

Beliefs the owner has locked cannot be merged, superseded, or contested.

---

## 8. After the pass — exporting and archiving

When all five steps succeed, the run writes a single file containing every active belief. This file
is what most of the rest of the system reads. Notably, the confidence written into that file is the
*computed band* from step three where one exists, not the confidence the model wrote in step two.

Separately, at 05:30, a second job archives. Beliefs marked deprecated — by fading, by the
reevaluator, or by losing a merge — are moved, along with their evidence, into archive tables in the
same database, then removed from the live tables. The merge records deliberately keep pointing at
those archived rows, so a surviving belief's history can still be traced through them.

---

## 9. How beliefs actually reach the assistant

There are two routes, and they work differently.

**The daily compile.** This is the main one. Once a day, and again whenever the inputs change
meaningfully, a step builds a document describing today: a series of time slots, each with a few
plain instructions, each citing the beliefs behind it. An agent called **dayflow_belief_selector**
picks which beliefs are relevant to today from the full set — again returning identifiers, with the
original records restored by code — and a second agent turns the selected beliefs plus the day's
context into the time-slotted document. The orchestrator that runs your day reads the slot it is
currently in.

This is worth understanding as a *compile* rather than a search. Beliefs are resolved into concrete
instructions once, by a model that can see the whole day at once, which means conflicts between
beliefs get settled once rather than re-argued at every step.

**Direct lookup.** There is also a function that returns beliefs ranked by a combination of
similarity to a query, recency, and how often the thing has been observed, optionally narrowed to a
set of tags. It is available to any caller. At present the meal planner is its only user.

---

## 10. The two other ways a belief can change

Outside the nightly run, two paths write beliefs.

**Your comments.** A routine reads comments you have left, hands them to an extractor agent, and
writes the resulting beliefs directly — bypassing the collection and updater steps entirely. These
are recorded as coming from a user comment.

**Work outcomes.** When a work object that was caused by a belief finishes, the outcome is
delivered back to that belief. An agent called **work_outcome** reads how the work ended and decides
whether the belief is unchanged, now resolved, or needs rewording; ordinary code applies it and
attaches the outcome as evidence. This is the only path by which something completing in the world
updates the belief that asked for it.

---

## 11. A note on the agents

Fifteen language-model agents exist in this subsystem. Ten are live and each appears above:
`belief_updater`, `evidence_match`, `belief_reevaluator`, `match_discover`, `match_review`,
`evidence_page`, `merge_check`, `belief_tagger` (which assigns the retrieval tags from a fixed
vocabulary), `work_outcome`, and `dayflow_belief_selector`.

The pattern they all follow is the same and is the most important thing to notice about this
subsystem: **an agent decides, and ordinary code writes.** Where an agent must refer to something
that already exists, it is given temporary local identifiers rather than real names, and the real
records are restored afterwards by code, so that a model cannot invent a plausible-sounding
reference and have it silently accepted.

The remaining agents are either experiment-only or retired; §11 and §13 of
[16_BELIEF_ENGINE.md](16_BELIEF_ENGINE.md) list which, and that list is maintained.
