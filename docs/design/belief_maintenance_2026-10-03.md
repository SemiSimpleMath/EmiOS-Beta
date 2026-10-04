# Belief maintenance (design, 2026-10-03)

Status: proposed 2026-10-03, no code. Builds on the evidence-weighted decay design the owner approved on
2026-05-11 (for the legacy belief engine, retired at the 2026-09-29 cutover) and on today's belief work:
atomic standalone beliefs, insights that keep their provenance, the calendar review, and contradictions held
for the owner.

The owner (2026-10-03): "We need not only intake we need belief maintenance that 1) merges duplicates, 2)
strengthens weakens based on evidence. 3) decays beliefs, deals with conflicting beliefs. beliefs can become
contested, beliefs can also evolve. these are things we have to handle".

## Where things stand

| Job | Today |
|---|---|
| New beliefs | Nightly intake: insights with their source entries → extractor (smart tier, atomic, standalone) → dedup (new / same / refines / contradicts) |
| Duplicates | Only at intake: a new atom judged `same` becomes evidence on the held belief. Owner edits are not checked, and the existing catalog has never been swept (B583 repeated B330 after an edit). `IntakeStore.merge` exists. |
| Strength | Evidence rows only: day, `relation` support / contradict, `kind` said / did. No weight, no band. |
| Decay | None since the cutover (bug list, 2026-10-03). |
| Contradiction at intake | Held `pending` for the owner (`IntakeStore.hold`, the review's third panel), built today. |
| Contested beliefs | Not recognised: contradicting evidence from the feedback path accumulates on a belief with no consequence. |
| Evolution | Only by owner edits. Work-outcome feedback (`belief_engine/work_feedback.py`) can still revise a belief on its own. |

## The rules

Evidence and contradiction (owner, 2026-10-03): "duplicate beliefs or mentions become evidence for a belief,
negative beliefs are not evidence for a belief they are new belief and have to be reconciled." A repeat adds
support to the belief it repeats; a statement against a belief is a new belief, held beside it until the owner
reconciles the two. No path writes contradicting evidence.

Reconciling is usually a rewrite (owner, 2026-10-03): the held belief's wording edited to fit both ("the owner takes the
dogs out in the morning" and "his wife took the dogs out" become "Some mornings the owner takes the dogs out, some mornings
his wife does"). The rewrite is drafted overnight and applied on the owner's word in the morning review ("maybe these
will again be things you get to approve in the morning"). The 2026-05-07 switch from intermittent fasting to
calorie-reduced meals shows the drafting works: the old automatic rewrite got all four beliefs right, and only
wrote "until <date>" history clauses into them (its prompt now writes the present state alone).

Code computes, agents propose, the owner decides. The single exception is retiring faded episodic and transient
beliefs, which are cheap to lose (owner, 2026-09-26: "if it is it will become a new belief later"). A false
merge is far worse than a duplicate (owner); approved calendar beliefs never change or leave the calendar without
the owner.

## 1. Strength: computed from evidence

A nightly pass (after the intake) recomputes, for every belief, from its evidence rows (the 2026-05-11 design):

- One weight: support. Each supporting row counts `weight × 0.5^(age_days / half_life)` (true half-life).
  The 2026-05-11 design kept a contradiction weight beside it; under the rule above a contradiction is a held
  belief, not a weight.
- Half-life by the belief's kind:

  | Kind | Half-life |
  |---|---|
  | `durable_fact` | none (only contradiction moves it) |
  | `stable_relationship` | 5 years |
  | `stable_preference` | 1 year |
  | `routine_pattern` | 90 days |
  | `episodic_context` | 14 days |
  | `transient_state` | 1 day |

- Row weight: the owner's own words (`said`, the owner's corrections) count more than what was inferred from
  behaviour (`did`); a later row counts more than an earlier one only through its age (owner: later and said
  evidence weighs more).
- A band from the support weight (thresholds from 2026-05-11): strong (> 4), medium (1.5–4), weak (0.3–1.5),
  faded (< 0.3). A belief with a held contradiction waiting against it is shown as contested, whatever its band,
  until the owner reconciles them.
- Row weight: the owner's words 1.0, behaviour 0.5, the assistant's own words 0.
- Stored on the belief as a snapshot (weights, band, computed day); the evidence rows stay the source of truth.
  Shown on /beliefs; retrieval ranks by it; the planner and the steward see it.

Evidence that keeps the band honest:
- An approved calendar belief gains support from its occurrences: a work outcome that shows it happened as
  stated is a support row (work-outcome feedback writes evidence, section 4).
- Rarely mentioned but important beliefs (trash night) are protected by approval, not by a separate salience
  score: an approved calendar belief that fades raises a question; it is never retired by decay.

## 2. Decay

- `episodic_context` and `transient_state` beliefs that reach faded are retired by code, with the band as the
  reason.
- `routine_pattern`, `stable_preference` and `stable_relationship` beliefs that reach faded go to the review as
  "fading: keep or retire?". Keep adds an owner `said` row ("still true"), which restores its weight.
- `durable_fact` beliefs do not decay.
- Approved calendar beliefs only ever reach the review.

## 3. Duplicates

- Candidates: pairs of active beliefs whose statement embeddings are at least a threshold apart (to be measured;
  B583/B330 were 0.885), found by code nightly and right after every owner edit.
- Judgment: `belief_engine::belief_dedup` on each pair, asked whether one adds nothing to the other.
- Proposal: a "Possible duplicates" panel in the review, both beliefs side by side with their evidence: keep A,
  keep B (`IntakeStore.merge`, evidence moves to the kept one), or keep both (remembered, not asked again).
- Never merged by code alone.

## 4. Contradictions

- Every statement against a belief becomes a held contradiction: a new belief, `pending`, beside the beliefs it
  contradicts (`IntakeStore.hold`), with a drafted rewrite of each that reconciles them
  (`agents.draft_revision`, `belief_engine::belief_revise`; none when the new belief was one occasion or the two
  cannot both be said). The review offers: apply the rewrite (edited first if needed; the held beliefs take the
  new wording and the new belief's evidence as support), keep the new one, keep the current belief, or keep both.
  Built for the intake 2026-10-03.
- The feedback path (`app/assistant/subconscious/feedback_extractor_persist.py`) attaches the owner's pushback
  to the belief as contradicting evidence today; it holds a contradiction instead.
- Work-outcome feedback (`belief_engine/work_feedback.py`) stops revising or retiring beliefs on its own: an
  outcome that confirms is support; one that shows the belief no longer holds becomes a held contradiction; one
  that adds a condition becomes a new belief (atomic: a rule and its exception are two beliefs). Its "qualify"
  rows (58 on 2026-10-03) count as support.
- The 37 contradicting rows already stored (35 in the owner's words; they made B99, B357 and B156 look
  contested on 2026-10-03) are re-raised once as held contradictions, each a new belief from those words.

## 5. Evolution

- A belief evolves through its held contradictions: the drafted rewrite says what the belief is now, and the owner
  applies it. When the owner keeps a new belief over an old one instead, the old one retires and the new one carries
  on. `belief_engine::belief_revise` drafts the present-state wording (its 2026-10-03 prompt: present
  state only, the history stays in the record).
- The review shows "Changed? old → new": approve, edit or reject. Approve is a revision with the drafted text as
  owner-approved evidence.

## The review, with maintenance

Panels in /beliefs/review (and the morning modal), each shown only when it has entries:

1. For the calendar (built)
2. New beliefs (built)
3. Contradictions (built)
4. Possible duplicates
5. Changed?
6. Fading

After the first sweep the daily volume should be small; the first run will not be, and may be worked through
over several days.

## What exists, what is new

| Piece | Exists | New |
|---|---|---|
| Evidence rows with day and relation | yes | a weight per row (said / did / owner) |
| Strength band | no | nightly snapshot columns on the belief |
| Decay | no (retired 2026-09-29) | the band's faded state, auto-retire for episodic / transient |
| Duplicate judgment | `belief_dedup` at intake | nightly and on-edit sweep, review panel |
| Merge | `IntakeStore.merge` | from the review |
| Held contradiction | built today | — |
| Contradictions from feedback and work outcomes | written as contradicting evidence | held contradictions |
| Revision drafting | `agents.draft_revision` drafts for every held contradiction (built 2026-10-03) | — |
| Work-outcome feedback | revises on its own | writes evidence and proposals |

## Open questions

1. The band thresholds and row weights: check them against the real catalog (how many beliefs land in each band
   on the first run) before anything acts on a band.
2. Duplicate threshold: measure on the current catalog how many pairs each cut-off surfaces.
3. Should "keep both" on a duplicate pair be remembered forever, or until either belief changes?
4. Do faded episodic / transient beliefs go to the review once before auto-retire starts, so the first run's
   retirements are seen?
5. Stored data: the snapshot columns change the belief table; the owner's go is needed at build time.

## Build order

1. Strength bands and decay (code only), with a first run reported to the owner before anything is retired.
2. The duplicate sweep and its panel.
3. Feedback and work outcomes hold contradictions instead of writing contradicting evidence or revising; the 37
   stored contradicting rows re-raised; the Changed? proposals.
4. The Fading panel and auto-retire for episodic / transient.

## First pass (2026-10-03, read only)

Over the 383 active beliefs, with both weights (before the evidence-and-contradiction rule): strong 16, medium
127, weak 204, faded 33, contested 1 (B156), overturned 2 (B99, B357). Facts and relationships did not fade; all
15 episodic beliefs faded; 18 of 148 routines faded. Of the 48 approved calendar beliefs, 5 faded, among them the
standup (B60) and the anniversary (B188): rarely mentioned, still true. A yearly date filed as a routine fades on a
90-day half-life; date-type beliefs need the fact kind.
