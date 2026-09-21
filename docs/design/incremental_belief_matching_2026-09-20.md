# Incremental belief matching

## Decision path

Evidence selection reads complete evidence pages against complete catalog pages. The LLM
chooses existing beliefs for the updater; selected context includes full stored evidence
and merge lineage. New beliefs are stored before matching; a proposed statement is never
assigned a fictitious observation date of today for a duplicate decision.

Each matching run computes content versions from claims, conditions, scope, kind, status,
locks and actual evidence. Processing timestamps, counters and exact repeated observations
do not invalidate judgments. Prompt/schema changes invalidate affected policy receipts.

The default budget is 20 dirty beliefs for discovery and 100 pending pair reviews per run.
Initial baseline coverage resumes over multiple runs. Discovery scans the whole active
catalog in complete-record pages, not arbitrary independent comparison windows: each focal
belief sees all pages. New/changed beliefs find relationships with unchanged beliefs too.
Unchanged pairs are not reviewed again. Pending counts are returned in pipeline step results.
No matching agents execute in ordinary chat.

## Complete context

Statements, conditions, evidence summaries and raw text are never clipped. Discovery pages
split between records; an oversized record remains whole. Review receives the complete two
belief packets and all stored predecessor evidence, including archives. Provider context
errors leave the review pending and raise; there is no silent clipping or guessed judgment.
Automatic multi-call investigation of a single oversized pair is not implemented.
Original conversations not retained in stored evidence are not fetched automatically; missing
source context warrants unresolved. Legacy inaccurate merges are not assumed correct.

## Durable state

Five additive tables are initialized on first use in the configured app database:
`belief_match_discovery`, `belief_match_pages`, `belief_match_pairs`, `belief_match_merges`,
`belief_observation_equivalence`. Existing tables and historical verdicts remain intact.
No live schema/data migration is run while installing the code.

Discovery pages and candidate queues commit together. A completed page survives interruption.
Pair decisions carry full input packets and rationale, bound to both versions and policy.
Unresolved is remembered until inputs change. Malformed answers are errors, not negative
judgments. Every mutation rechecks versions under BEGIN IMMEDIATE and commits with receipts.
Owner locks are enforced even if a model proposes otherwise.

Merges retain the source beliefs/evidence via redirects, including after archival; ordinary
BeliefStore.get_evidence and confidence recomputation follow those links. No merge-generated
support or new confirmation date is added. Earliest/latest observed dates come from sources.
Tags are retained. Exact observation repeats count once. A model may additionally identify
same-source observation groups; structurally conflicting dates/signals are refused. Originals
remain available. Similar agreeing observations are never automatically called duplicates.
Confidence is recomputed after merges; legacy bookkeeping weights are ignored.

Supersession requires the model to identify the current side and cite sources. Contradictions
become contested and persisted contested beliefs are picked up for reevaluation next run.
Index synchronization retries from durable receipts after commit, including after a restart.

## Verification and limits

Isolated SQLite tests cover unchanged nights, evidence/condition/policy changes, resumable
budgets, complete source text, unknown/invalid decisions, uncertain pairs, rollback, stale
writes, locks, original dates, archived lineage, evidence deduplication and index recovery.
These are implementation checks, not a measurement of model recall or precision on the live
catalog. No private catalog was sent to an external model for this implementation.

The older updater still advances counters/confirmation dates when rereading evidence (MEM4);
matching ignores that metadata churn. Historical merge damage cannot be repaired automatically.
Weekly belief ingestion and full source-document retrieval remain separate outstanding work.


Synthetic standard-agent checks also exercised the actual provider schema: true paraphrases
were classified same with independent support retained, a morning-only preference remained
specialised relative to a broader preference, and different reminder subjects stayed distinct.
An additional stronger "always" claim was conservatively retained separately. These examples
do not establish recall/precision across the live catalog. Unrestricted conditions objects
were rejected by provider structured output; conditions now travel as validated JSON text,
with the full JSON object preserved in storage. No qualifier vocabulary is imposed.


Dry-run preparation found that embedding full evidence lineage in every discovery focal
record multiplied the initial 579-belief baseline into 3,632 model calls. Discovery now
reads complete belief records (including uncut statements and conditions) against catalog
pages; pair review still receives complete source evidence and archived lineage. Evidence
changes still invalidate discovery/review versions. No text fields are clipped.


## Full-catalog evaluation follow-ups

- Discovery validates exact supplied identities, removes self-pairs, and accepts candidate pairs within either supplied set. Invalid IDs get one retry using compact page-local labels. Review similarly retries invalid evidence citations using exact source labels, with unchanged text.
- Proposed `same` and `supersedes` decisions pass a separate `belief_engine::merge_check` agent. It compares practical contextual meaning in both directions and rejects compound supersets, changed applicability, missing qualifiers, or new permissions. Invalid approval preserves both claims with diagnostics. The complete proposal remains in the receipt.
- Packets over the full-review budget use `belief_engine::evidence_page` to read every lossless source fragment. Page receipts are keyed by policy and complete input. Reviewer and merge checker share cached source findings. All findings are retained; if they still cannot fit, the pair remains pending. No character truncation is used.
- Prompt/schema policy changes invalidate comparison receipts; unchanged source/version/policy comparisons do not call a model again. These operations run in the background belief pipeline and add no ordinary-chat model calls.
- The personal-data evaluation was explicitly authorized by the user and used isolated database copies. No evaluation merge, status change, vector update, or provenance repair was applied to live data.

For supersession, the final checker validates dated replacement and checks that retiring the entire old record loses no independently valid clause. A partial correction to a compound belief stays unresolved for source-based splitting or revision.


## Production updater and Dayflow projection size repair — 2026-09-21

Evidence selection now returns complete current BeliefRecord values, not hydrated
packet() histories for every selected belief. The updater processes whole incoming
records in chronological batches, retaining local evidence references and selecting
against current store state for each batch. A pre-call 180,000-byte UTF-8 input guard
recursively splits multi-record batches; an individually oversized source stays pending
with an explicit error. This guard budgets supplied data; it is not an exact tokenizer
or a universal guarantee for arbitrary injected system resources. No source text is cut.
The prompt preserves unaffected clauses/conditions and directs uncertain historical
rewrites toward separate candidates for source-aware reconciliation.

Dayflow selects from complete current exported beliefs in whole-record catalog pages.
Each page sees the same whole-day calendar/context and historical weekly context.
The selected union goes to the existing writer, which places guidance into hour slots
and carries cross-cutting restrictions. It does not perform one database comparison per
time slot. Existing context/catalog fingerprints still skip unchanged projections.
The writer's selected union remains one prompt; exceptionally large selections remain
a scaling limitation rather than being silently truncated.


When complete pair-page findings exceed the final review budget, retain a
pending_review_budget receipt, with applied=0 and the budget recorded. The pair key
already binds source versions and policy. Other comparisons and export continue;
blocked_review_budget exposes this incomplete work. Unchanged blocked pairs do not
repeat model calls; source/policy/budget changes permit another attempt. This is not
a merge decision and does not imply that the entire deduplication backlog is complete.
A strategy for reviewing exceptionally large accumulated findings remains future work.
