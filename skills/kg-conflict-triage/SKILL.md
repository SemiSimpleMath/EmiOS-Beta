---
name: kg-conflict-triage
description: Combined doctrine + playbook for triaging a reported KG/wiki/card conflict. KG schema invariants, wiki/card projection rules, triage flow, mutation gates, escalation. Use whenever processing a contradiction, stale_content, wiki_contradiction, or duplicate_node finding.
license: Apache-2.0
metadata:
  author: jukka
  version: "3.0"
  applies_when: "kg_resolution_manager / kg_investigation_manager (any agent acting as KG repair or triage surface)"
  auto_inject_when:
    task_keywords:
      - "kg_maintenance_finding"
      - "wiki_contradiction"
      - "stale_content"
      - "duplicate_node"
      - "state_contradiction"
      - "resolve cluster"
      - "fix the kg"
      - "investigate finding"
      - "contradiction"
---

# KG conflict triage

## Meta-principle

**KG is the source of truth; wiki pages and entity cards are projections.**
Every reported conflict is exactly one of:

1. **KG bug** — a node/edge is wrong (open era should be closed, dates
   inverted, missing edge). Recommend the smallest KG mutation.
2. **Stale projection** — KG correct; the wiki/card hasn't refreshed.
   No mutation needed; regen happens downstream.
3. **False positive** — the texts don't actually conflict, or the reporter
   misread the schema. Dismiss with an educational reason.

Never a fourth ("the projection is right because it says so") — KG wins.

## Step 0 — is the conflict real?

Before any SQL: put the two allegedly-conflicting statements side by side
with the context you were already handed (quoted texts, subject node +
edge neighborhood, entity one-liners). If they reconcile on plain reading,
that is bucket 3 — at most ONE confirming query, then stop. Reconciling
patterns (all bucket 3):

- **Same thing, two names** — the "two activities/venues/groups" are one
  thing described twice (e.g. a weekly gathering that IS the Zoom call).
- Closed era read as current — "X takes Y" vs "X stopped Y"; check `end_date`.
- Date precision mismatch (`2024-03-15` vs "around 2024").
- Source-relative phrasing ("last year") with no anchor.
- Two same-label States, different windows = sequential eras, both correct.
- Recurring same-label Events = separate occurrences (the recurring-event
  trap) — never a merge target just because labels match.
- Same-label Properties on different subjects = correctly separate.

## Schema essentials

- Entity / Concept = timeless identity. State = era with a validity window
  (`end_date IS NULL` = open). Event = point-in-time / bounded. Goal carries
  `goal_status`. Property is subject-scoped, never global.
- `label` and `original_sentence` are present-tense BY FORM. Truth-as-of-now
  lives in `start_date`/`end_date`: end in past = historical; end null +
  start set = open; both null = soft claim (don't assume current); future
  start = planned.
- Date confidence: `auto_decay` = a decay guess, re-openable; `user_set` /
  `explicit` = authoritative. `*_prose` carries the fuzzy form.

## Projection rules

- Cards and wiki pages regenerate automatically after KG mutations — never
  recommend regen explicitly, and always mutate KG before any regen happens.
- Closed States render past tense on cards; they don't disappear. A card
  fact contradicting a *closed* State is staleness (bucket 2); a card
  reflecting a *still-open* State that prose says ended means the KG is
  wrong (bucket 1: close the State).

## Data-quality red flags (check on every cited node)

- `start_date > end_date` (inverted interval — usually a legacy import)
- future `start_date` on a past-tense `original_sentence`
- `start_date` exactly equal to `created_at` (defaulted, not observed)
- `start_date = end_date = YYYY-01-01` with confidence `estimated` and
  prose like "in YYYY" — a year-floor placeholder: only the YEAR was
  ever claimed; January 1 is fabricated by the date estimator. Treat it
  as year-precision. NEVER propagate it to another node as an exact
  day, and never let it outvote an `actual`/`user_set` date about the
  same life event.

Any of these on a cited node IS the real bug — bucket 1, regardless of
what the finding claimed.

## Triage flow

0. Step 0 above — most findings die here.
1. Read the finding's actual disagreeing texts, not just its `reason`.
2. Query the cited nodes (label, sentence, dates + confidence + prose,
   created_at); run the red-flag check.
3. A schema invariant explains the conflict → bucket 3. Otherwise bucket 1
   (name the smallest mutation: ids, fields, dates — the executor picks the
   tool) or bucket 2 (no mutation; data correct).
4. Write the verdict so the next investigator hitting the pattern learns it.

High-stakes calls only (merges, near-irreversible mutations): walk
provenance to the verbatim source — `kg_node_evidence.window_id` →
`kg_window_message` → `unified_log_2026` — before recommending. Skip for
low-stakes triage.

## Stop early

After every result ask: "could I write the verdict NOW, with dates
sanity-checked on every cited node?" Both yes → stop immediately. 2-4
actions typical; a step-0 false positive should close by action 2; 8 is
the ceiling. Stop signals: re-querying nodes already cited, repeating an
earlier query, "just one more check", schema archaeology (PRAGMA /
sqlite_master) on tables outside your cheatsheet.

## Escalate when

- Prose contradicts existing dates and authority is unclear.
- The fix is near-irreversible and needs user approval (merging two
  evidence-rich nodes; deleting a connected hub).
- Cluster findings point opposite directions, or blast radius is unclear.

Escalations must pose ONE specific question the user can arbitrate.

## Discipline

Read before recommending. One mutation per recommendation step. Educate in
dismiss reasons. Finding producers (wiki critic, scanners, ttl estimator)
propose but don't mutate — deciding when the FINDING is the misread is
part of your job.
