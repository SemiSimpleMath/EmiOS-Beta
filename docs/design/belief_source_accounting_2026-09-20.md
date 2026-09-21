# Belief source accounting — 2026-09-20

## Implemented contracts

- Claim changes and all attached evidence commit in one SQLite transaction. Exact replay checks run under the same write lock. Vector indexing follows the commit; a failed index update raises and leaves both the claim and evidence intact.
- Observation count comes from original source records through merge lineage and reviewed equivalences. Rewriting a claim does not create an observation. First-observed and last-confirmed use source dates; contradicting evidence does not reconfirm the claim. When original dates are unavailable, existing legacy dates are retained rather than invented.
- Owner-locked claims retain their content. An omitted conditions field preserves existing conditions; explicit empty conditions can clear them.
- Global nightly collection admits complete weekly candidates, including conditions and source descriptions. Weekly interpretations carry zero independent support and are excluded from observation counts. The updater reconciles them with original daily evidence. Per-domain diagnostic collection excludes weekly candidates because their semantic area requires the global updater. Calendar projection uses the reconciled catalog instead of injecting weekly candidates directly.
- Decay uses each source's saved half-life. New durable evidence records -1 for no decay; legacy NULL falls back to the current kind because the original policy cannot be recovered. Source dates, not ingestion dates, determine age. Zero weights remain zero.
- Missing or undated usable evidence yields `unverified` with zero weights. It does not automatically mean the claim is false or deprecated. Canonicalization, deprecation, and staleness-review bookkeeping cannot reinforce beliefs.

## Verification and rollout limits

Isolated tests cover replay, evidence rollback, indexing failure and retry, conditions and locks, zero-weight weekly context, dates, and half-life preservation. No live database recompute or model evaluation was performed for this change. It affects the background belief pipeline, adding no model call to ordinary chat.

A read-only audit of the copied baseline found 131 active claims without original observations through recorded lineage, and no same-key archived source records to restore automatically. Source recovery remains a separate investigation.

Remaining: overlapping ticket rollups still need source-event accounting; evidence-to-claim polarity needs LLM interpretation; ordinary vector writes lack a durable repair outbox; legacy lost provenance cannot be invented; oversized review needs multiple passes; full personal-data deduplication evaluation awaits explicit approval after automatic review blocked it. Daily legacy source identities are preserved to avoid adding another copy during migration.


## Follow-up: interpretation and rolling ticket windows

New ticket windows retain full source records with zero independent support, just as weekly interpretations do. They are context for the model, not additional observations. Each updater citation now includes an explicit LLM-assigned evidence-to-claim valence. Python validates coverage and identifiers, then persists that relationship. Expiration alone no longer produces a snooze/rejection conclusion. Historical weighted rollups and previously misassigned valences still need source review; this change prevents new instances.

## Historical provenance recovery investigation

The frozen-baseline review found relevant source proposals for 121 of 131 missing-source
claims, including partial attributions. API credit exhaustion interrupted the review; 603
source relationships, 31 source-only checks, and 155 conflicting judgments remain pending.
These are proposals, not restored live provenance or refreshed confirmation. Many claims
combine factual observations and derived assistant guidance, which needs whole-claim review.
Nine focused tests pass for the opt-in proposal service. Its private evaluation runner now
uses stable source ordering; completed judgments were recovered offline by exact context.
Private copied data and reports remain outside Git. No ordinary-chat model calls were added.

### Continuation completed

After the API credit top-up, all 20,762 candidate source relationships have initial
assessments and the outstanding source-only checks are complete. Relevant source proposals
exist for 122/131 missing-provenance claims. The previous 603/31 source/check backlog is
cleared. The remaining 155 disagreements concern four beliefs; a fifth broad-versus-specific
contradiction flag is held for contextual review. No live data was changed. A repeated
continuation schedules zero model calls and preserves the exact review index.
