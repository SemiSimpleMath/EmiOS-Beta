# Belief engine — defects, 2026-09-26

Companion to [BELIEF_ENGINE_WALKTHROUGH.md](../architecture/BELIEF_ENGINE_WALKTHROUGH.md), which
describes how the system works. This is what is wrong with it.

Produced by four parallel code audits (forming, merging, decaying, contradictions), each reading
its area end to end, plus direct measurement against the live `emi.db`. Nothing here has been
fixed; no code was changed.

**How to read the confidence markers.** Claims are not equally warranted:

- **VERIFIED** — I read the code myself and confirmed it.
- **MEASURED** — I ran the query against the live database; the number is from the data.
- **AUDIT** — an audit read the code and reported it; I did not personally re-check.

Treat AUDIT items as strong hypotheses. Verify before writing code against one.

---

## Fix these three first

1. **§1.1** — your comments are being consumed and discarded. Active loss of the rarest evidence
   type in the system.
2. **§2.1** — a merge can silently erase a belief's structured conditions. Ongoing, and it destroys
   exactly the applicability data we have almost none of.
3. **§1.3** — ticket replies stopped becoming weighted evidence on 2026-09-19. Your most direct
   feedback channel is disconnected from beliefs.

---

## 1. Forming

**1.1 — Five of seven feedback-extractor domains do not exist, and the comment is consumed anyway. VERIFIED.**
The extractor may emit `meal, wellness, romantic, scheduling, communication, household_routine,
other`. The configured domains are `communication, food, general, health, meal, routine, sleep,
work`. Only `meal` and `communication` overlap. `upsert_belief` raises on an unknown domain,
`feedback_extractor_persist.py` catches it at WARNING, and the source comment pod is marked
processed regardless — so the belief is lost *and* it never retries. There are 20 `user_comment`
evidence rows in the entire database; this is the rarest and most direct evidence the system has.

**1.2 — A belief can be created with no evidence at all, at high confidence. AUDIT.**
`evidence_refs` defaults to empty; with empty references and empty relations the validation passes,
and the row is written. `status` and `confidence` are taken verbatim from the model with no
minimum-evidence bar. The only counterweight is the computed band, which assigns `unverified` and
then does nothing with it (§3.1).

**1.3 — Ticket replies stopped producing weighted evidence on 2026-09-19. VERIFIED + MEASURED.**

```
ticket_rejection    137 rows   2026-03-03 .. 2026-09-19    stopped
ticket_acceptance    49 rows   2026-03-03 .. 2026-09-19    stopped
ticket_context       42 rows   2026-09-20 .. 2026-09-25    replaced them, weight 0.0
```

`ticket_context` is an aggregate blob grouped by ticket type, carries `weight=0.0`, and is listed
in `NON_OBSERVATION_SOURCES`, so it contributes nothing to strength and is not counted as an
observation. Weight-zero is correct for an *interpretation* that would double-count — that is why
`weekly_insights` is zero — but a ticket reply is a primary observation. This looks like a swap
that lost something rather than a deliberate demotion; worth an owner ruling before it is "fixed".

**1.4 — A belief-key collision silently overwrites an unrelated belief. AUDIT.**
`belief_key` is unique; `upsert_belief` looks up by key and overwrites statement, confidence, scope
and status wholesale. A `create` whose key already exists is rewritten to `update` with no
similarity check, so a hallucinated key that happens to match replaces the belief that owned it.

**1.5 — Two lanes, two key namespaces, no shared registry. AUDIT.**
The nightly updater and the feedback extractor use different key conventions with nothing
reconciling them.

**1.6 — User comments weigh less than machine-mined insights. AUDIT.**
A comment is weighted 0.3–1.0 by confidence; a mined daily insight marked chronic is weighted 3.0.
Four explicit high-confidence comments are needed to reach what two mined lines reach.

**1.8 — Legacy beliefs carry whole days of unrelated evidence. MEASURED 2026-09-26.**
`routine.kids.wednesday_late_start_no_7am_wakeup` has 112 observations recorded directly on it
(not via merges): coffee snoozes, Gmail cleanup, news-site browsing, dog breaks — almost none about
Wednesday late start. `communication.calendar.surface_irregular_events` has 23, of which about 22
are unrelated. All such rows are dated 2026-03 and have `extracted_by` NULL, i.e. they predate
per-citation evidence attachment: the early updater attached a whole evidence batch to a belief.
Found independently by three of the ten belief reviewers (chunks 1, 2, 8), so it is not isolated.
Consequence: observation counts and support weight on these beliefs are fiction, and they rank as
strongly supported. Frozen legacy damage, not an ongoing leak (current writes require an
evidence-to-claim relation per citation). Status: recorded, not fixed.

**1.7 — `BeliefUpsertRequest.first_observed` / `last_confirmed` are inert. AUDIT.**
Never read by `upsert_belief`; both are derived from evidence after the write. Three call sites set
them believing otherwise.

---

## 2. Merging

**2.1 — A merge can erase the survivor's structured conditions. VERIFIED.**
`history.py:81-85` binds `conditions` to the reviewer's `conditions_json`, and
`match_review/agent_form.py:12` declares that field `str | None = None`. When the reviewer omits it,
the survivor's existing conditions are set to NULL. There is no mechanical diff protecting them —
only a prose instruction to the second-opinion agent. Given that only 33 of 612 beliefs carry
conditions at all, this destroys the scarcest field in the store.

Statement, scope and kind are also overwritten wholesale; the loser's conditions are dropped
outright; `confidence` and `domain` are never reconciled between the two.

**2.2 — There is no un-merge. AUDIT.**
Confirmed by exhaustion across the repo. The pre-merge snapshot exists (`belief_match_merges.before_json`,
4 rows) but nothing reads it except index syncing. Recovery would be manual SQL.
For contrast, the knowledge graph treats merge reversal as first-class — it has a merge log with an
`undone_at` column and captures row-level ids specifically so an unmerge can reinstate them. Same
codebase, two standards for the same operation.

**2.3 — `supersedes` writes no merge-link row. AUDIT.**
It deprecates a whole belief without recording which belief replaced it. After archiving, that
record's evidence is reachable only by reading the archive tables directly — the same shape as the
historical orphaning defect in §2.5, still present in current code by design.

**2.4 — An archived loser's `belief_key` is freed for reuse. AUDIT, consequence INFERRED.**
Archiving deletes the deprecated row, releasing the unique key. A later belief can be minted on the
same key with no lineage to the absorbed one — resurrecting it as a fresh record.

**2.5 — 156 survivors carry an absorption record with no merge link. AUDIT.**
Those records name 306 absorbed belief keys, all of which resolve to archived rows holding **1,331
archived evidence rows**, none of which appear as a merge loser. That evidence is unreachable from
any live belief. Every such record is dated 2026-03 to 2026-06 and belongs to the retired
canonicalizer — frozen damage, not an ongoing leak. Current merge code does not orphan, and that is
backed by regression tests.

*(My own narrower measure, MEASURED: 137 active beliefs have no user observation anywhere in their
lineage, 109 of them being merge survivors with no link. Different population, same root cause.)*

**2.6 — `belief_key` is never updated by a merge. AUDIT.**
The survivor keeps its original key, which may now misname the combined claim.

**2.7 — One user statement counts as many observations after merges. MEASURED 2026-09-26.**
`routine.work.boundary.no_seyfarth_environment_access` reaches 42 "real" observations through its
lineage, but they are two user messages (2026-06-27 and 2026-07-29), each copied onto 14 and 28
separate beliefs that were later merged into this one — 41 distinct belief ids, weight 3.0 each.
All 42 rows have `source_ref` NULL, and `context.observation_key` deliberately keys unlinked
evidence by row id ("identical wording without source identity can be two independent
observations"), so `observations()` never collapses them. Consequence: merge lineage inflates
support roughly 20x for this belief, and any consumer of `observations()` (the atomizer harness,
merge review, a future evidence-distributing writer) is handed dozens of copies of one statement.
Status: recorded, not fixed. Deciding when same-date, same-text daily-insight rows are one
observation is a design call.

---

## 3. Decaying

**3.1 — `unverified` is an invented band that nothing acts on. VERIFIED + MEASURED.**
The declared band type is `high | medium | low | faded | contested | deprecated_by_contradiction`.
`unverified` is written by the recompute step but is not in that list and is not in the
contested-keys filter. **149 active beliefs** are parked in it.

**3.2 — An evidence-free belief has no exit route at all. AUDIT + MEASURED.**
Only `faded` deprecates and only `contested` / `deprecated_by_contradiction` flip status; `high`,
`medium`, `low` and `unverified` are inert (VERIFIED at `recompute.py:199-217`). A belief with no
countable evidence never accumulates weight, so it can never fade. The two paths that retired
beliefs on *last-confirmed date* rather than weight — `decay_temporary_beliefs` (30-day) and
`flag_stale_chronic_beliefs` (180-day) — were unwired when `DecayStaleBeliefsStep` was retired.
That is why the 137 cannot age out. Curation is currently the only mechanism that can remove them.

**3.3 — `effective_weight` is a dead column. MEASURED.**
0 of 612 rows populated. No writer, no reader.

**3.4 — Stored `confidence` and computed band diverge permanently. AUDIT.**
Decay never writes `confidence`. Which value a consumer sees depends on its path: the exported
resource prefers the band, the retrieval function reads the stale column, the admin UI shows both.
The same belief can be `high` to one reader and `low` to another.

**3.5 — `last_contradicted_at` is write-only. AUDIT.**
Set with COALESCE so it never clears; no reader anywhere.

**3.6 — Re-mentioning a finished event resurrects it. AUDIT.**
Every fresh supporting row restarts the half-life clock, so an episodic belief can be held at `high`
indefinitely. There is **no completion signal** anywhere in the decay path — a finished event and a
merely unmentioned one are indistinguishable to the model. Settled events do eventually leave, by
ageing out roughly a month after their last observation.

**3.7 — `kind` is assigned once by prefix heuristic and never revised. AUDIT.**
`kind` picks the half-life, so a settled event misfiled as a stable preference gets 365 days instead
of 14. Keys ending `.closed` / `.completed` are not in the heuristic's table.

**3.8 — `scope` is now nearly vestigial. AUDIT.**
It still drives `kind` at creation but, since the retired decay step was its only other consumer,
nothing else reads it.

---

## 4. Contradictions

**4.1 — A user contradiction is recorded and then ignored. AUDIT.**
The feedback extractor does provide a fast path: it writes `user_comment` evidence and routes a
contradiction through the guard that can only weaken an existing belief. But it attaches the
evidence and stops — it never marks the belief contested, never queues a re-evaluation, never
touches status. The belief keeps being served unchanged until the next nightly recompute, and
flipping to contested requires contradiction weight above 2.0 against support above 2.0, which a
single 0.3–1.0 comment cannot reach. **You can flatly contradict a high-confidence belief and have
it carry on being used, with your objection sitting inert in its evidence trail.**

**4.2 — Two active beliefs that flatly contradict each other are never noticed. AUDIT.**
Cross-belief contradiction is only detectable through the merge-discovery path, which is bounded to
20 focal beliefs and 100 pairs per run and only re-examines a belief when its content changes. A
stable contradicting pair that discovery never proposes is invisible indefinitely.

**4.3 — Contesting a belief is close to silent deletion. AUDIT.**
A contested belief is excluded from retrieval, excluded from future merge review, and has no
evidence row written on either side recording the conflict. The re-evaluator then sees each belief
separately, from its own trail, with no indication the other exists — and can restore both to
active.

**4.4 — `mark_contested` has no guards. AUDIT.**
Unlike `deprecate`, it checks neither the owner lock nor the current status, so a locked belief can
be flipped to contested and thereby dropped from retrieval.

**4.5 — Canonicalization runs after re-evaluation. AUDIT.**
A `contradicts` verdict therefore waits a full pipeline run before anything acts on it.

---

## 5. Retrieval and delivery

**5.1 — The retrieval function ignores decay entirely. AUDIT.**
`beliefs_for_context` applies no band filter and no net-weight term, and ranks on its own unrelated
30-day recency curve while reading the stale `confidence` column. A faded-but-not-yet-gone belief
ranks like a live one.

**5.2 — One principled retriever, one consumer. MEASURED.**
`beliefs_for_context` is used only by the meal planner. Five other consumers each read the 533 KB
exported file and roll their own filtering. Nothing improves while there are six implementations.

**5.3 — 137 unauditable beliefs are in active circulation. MEASURED.**
137 active beliefs have no user observation in their lineage; 100 are high confidence; all 100 are
in the exported file every agent reads; **14 are cited in today's routine document** as live
directives. Most are probably true — they are unauditable, not wrong — but nothing distinguishes
them from a belief with twenty observations behind it.

---

## 6. Legacy still resident

All confirmed. Note that §11 and §13 of `docs/architecture/16_BELIEF_ENGINE.md` already document
most of this map and are accurate; the items below add size and consequence.

| What | Size | State |
|---|---|---|
| `belief_engine_v2/` | **46 MB** — a 6,275-belief seed database, two ~9 MB working databases | zero importers; the reference doc says it awaits removal |
| `store/distinct_pairs.py` + `belief_distinct_pairs` | 68 lines, **9,150 rows** | zero importers, zero writers; `db/schema.py:106` recreates the table on every fresh install |
| `pipeline/steps/decay_stale_beliefs.py` | 116 lines | out of the pipeline since 2026-05-11 **but still executed by the sandbox** |
| `state/sweep_tracker.py` | 140 lines | dead; its docstring describes a canonicalizer cadence that no longer exists |
| agent `belief_canonicalizer` | full agent | orphan in both trees; wrote the 2,602 canonicalization rows including the 306 orphaned absorptions |
| phantom `source_type` vocabulary | — | `ticket_rejection`, `ticket_acceptance`, `manual_seed`, `kg_edge` carry weights in the decay table with no live producer |

**6.1 — The sandbox validates a decay model production does not run. AUDIT.**
`DecayStaleBeliefsStep` is still executed by the sandbox and five of its scenarios. Those scenarios
pass while the shipped model behaves differently. Anyone reasoning about decay from the sandbox is
reading the wrong implementation.

**6.2 — `merge_verifier` is not an orphan.** It belongs to the v2 tree and is called from
`belief_engine_v2/ingest.py`. It is unreferenced by the live v1 pipeline, which is a different
claim. Recorded because an earlier pass of this audit got it wrong.

---

## 7. Cross-cutting observations

**7.1 — Confidence carries almost no information. MEASURED.** 499 of 612 active beliefs are
`high` — 82%. A field where four in five values are the same cannot discriminate, and it is the
field the retrieval path reads.

**7.2 — Applicability is not recorded. MEASURED.** 33 of 612 beliefs have structured `conditions`;
15 of those 33 are a prose blob in a JSON wrapper. Retrieval can therefore only match on subject,
never on whether a belief governs the situation at hand — which is the mechanism behind the
thermostat, vet-reminder and dog-walk false positives of the past week.

**7.3 — Statements are long. MEASURED.** Median 49 words, 384 beliefs over 40 words, 84 over 80,
longest 278. The whole set is ~32,600 words. Much of the length is applicability crammed into prose
because `conditions` had nowhere to put it (§7.2).

**7.4 — 11% of the set is near-duplicate. MEASURED.** 43 pairs above a 0.42 similarity threshold,
involving 69 beliefs, including one exact-duplicate pair whose keys differ only by a dot versus an
underscore. Real, worth cleaning, but not structurally significant — deduplication takes 612 to
about 570.
