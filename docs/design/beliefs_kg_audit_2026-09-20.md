# Beliefs and Knowledge Graph audit

Date: 2026-09-20. Scope: current local code, including the uncommitted Dayflow repairs.

## Assessment

**Keep the two representations, but do not treat the current combination as a reliably
coherent memory system yet.** The strongest foundations are source-preserving KG
proposals, temporal relationship modeling, explicit finalizer outcomes, evidence trails,
and the belief layer’s attempt to represent uncertainty and forgetting. The weak point
is the contract between observations, inferred claims, corrections and what readers see.

This is not simply short-term beliefs versus long-term KG. Both store long-lived and
short-lived information. They currently act as partly independent interpretations of
the same user, with no shared claim identity or general cross-store reconciliation
found in the reviewed paths. More model intelligence cannot recover context the
writer discarded, distinguish events a collector mislabeled, or retrieve dates
that the reader omitted.

This audit discovered substantial memory defects outside the recently repaired Dayflow
list. The earlier assessment that the known major Dayflow bugs were nearly exhausted
should not be read as a clean bill of health for these subsystems.

## What the systems actually do

| Dimension | Belief engine | Knowledge graph |
|---|---|---|
| Representation | Mutable prose claim keyed by domain/key, evidence rows, kind, confidence, conditions, tags and owner lock | Entities, states, events, goals, properties and typed edges; validity/observation fields and source evidence |
| Main producer | Enabled v1 global nightly pass; v2 is not the primary producer | Chat resolver → topic windows → critic/extractor → enrichment → proposals → promoter |
| Inputs | Last 14 calendar days of generated daily insights and ticket aggregates; feedback extractor also writes directly | Eligible master-room user/assistant conversation context; critic selects user lines to extract; explicit tools/maintenance can also mutate KG |
| Meaning | Mix of explicit preferences, inferred habits, constraints, identity facts and temporary states | Entity identity, relationships, events, temporal claims, goals and provenance |
| Time | Kind-dependent evidence decay: transient 1 day, episodic 14 days, routine 90 days, preference 365 days, relationship 1825 days, durable facts no decay | Start/end dates and source observation times; estimated State/Event TTL with grace period; historical eras can remain in the graph |
| Updating | Model create/update/deprecate decisions, write-time dedup, evidence snapshot, reevaluation, periodic pairwise sweep | Identity resolution and semantic/participant candidates, model-assisted merge, proposal application, temporal conflict handling and maintenance |
| Agent reads | Exported JSON for routine/health/entertainment consumers; live ranked DB retrieval for meal context | ask_kg cited RAG, entity cards/wiki, entity convergence and background context memo |
| Retirement | Deprecation removes from active views; daily archive moves rows/evidence out of live tables | Ended states remain historical; maintenance can close/merge/delete with separate controls |

The belief pipeline is configured for 00:30 local. That cadence is appropriate for
pattern inference; it is a poor primary home for “right now” information. A one-day
half-life does not make a nightly ingestion path an immediate memory path.

### The end-to-end memory paths

1. **Beliefs:** daily insights/timeline files → evidence bundle → updater with one
   batch-level semantic lookup (top 8 existing beliefs) → belief/evidence persistence
   → evidence-weighted snapshot → reevaluator → canonicalization → exported resource.
   The live meal reader uses a different retrieval/confidence path. Feedback comments
   can enter through a direct writer. Noticer belief updates currently stop at logging
   (MEM1), even though the noticer prompt instructs it to emit them.
2. **KG:** raw log → resolved references → conversation window → critic-selected user
   lines → extracted graph → enriched proposal → promoter identity/merge/conflict
   handling → live graph plus indexes → projections/RAG. The graph has stronger raw
   source linkage than the belief insight path, which often supplies no source_ref.
3. **Concern handling:** concerns and completed work are another state layer. DF40 now
   sends new concern-linked closures back durably and makes handling history visible
   to the noticer. That does not automatically synchronize a matching belief or KG
   claim. A work completion is also not automatically proof the underlying need ended.

## What is good

- KG proposals separate extraction from promotion. Identity resolution, user locks,
  temporal succession checks and evidence joins provide useful controls.
- Keeping historical eras instead of overwriting every relationship is the right
  foundation for answering both “now” and “what used to be true.”
- Beliefs distinguish several decay horizons and evidence conflict from simple age.
  The evidence dedup guard already prevents identical rows being added every night.
- Cross-domain dedup, explicit merge verdicts and remembered distinct pairs are
  better than treating semantic similarity as identity by itself.
- The recent work-result/finalizer and concern-feedback repairs provide a useful
  pattern for reliable, idempotent memory updates after real outcomes.

These strengths justify repairing the system rather than replacing it wholesale.

## Confirmed findings

P1 means prioritize before relying broadly on unattended memory-driven actions.
P2 means a significant reliability or observability defect. These priorities concern
the reachable code behavior; this audit does not establish live incident frequency.

### MEM1 — P1: Noticer belief updates are logged and counted, never applied

The noticer schema and prompt permit belief_updates, and its routine reports their count. The writer reads that list but only counts it and includes the original output in the tick log; it never invokes a belief writer. The routine calls no subsequent belief-application stage. A user correction understood by the noticer can therefore fail to become memory.

**Evidence and direction:** Confirmed by call-chain inspection and a temporary-register/database probe: one reported update, no belief row. Apply through the normal belief writer with evidence and a durable delivery receipt; distinguish emitted, applied and failed counts.

**Primary code:** [app/assistant/subconscious/persist.py:169](../../app/assistant/subconscious/persist.py#L169).

### MEM2 — P1: Ticket outcomes are converted into stronger user-intent claims

The aggregate collector maps expired to snoozed and emits “User snoozed/deferred ... without completing.” It also maps accepted to acceptance without reading the contextual response meaning, scope or typed text introduced by DF36. An acknowledgment can therefore become acceptance; absence of a reply becomes a claimed user decision. Thresholds use event count, not distinct days, despite the cross-day description.

**Evidence and direction:** Expiry misclassification reproduced with three expired tickets. Accepted-response ambiguity confirmed by the collector fields it reads. Use explicit response semantics, retain silence as silence, and separate receipt from commitment/completion.

**Primary code:** [belief_engine/pipeline/steps/collect_evidence.py:212](../../belief_engine/pipeline/steps/collect_evidence.py#L212).

### MEM3 — P1: Evidence polarity is assigned before the belief it is evidence for exists

The updater attaches the source signal unchanged to every cited belief. rejects becomes contradict in the store, even when the belief is “the user does not want this suggestion.” That observation supports the negative preference. Conversely, a chronic daily-insight item defaults to confirms even when it contradicts the specific existing belief being updated. The output schema has evidence indices but no per-belief evidence relationship.

**Evidence and direction:** Reproduced a negative preference supported by rejection: recompute gives it zero support, positive contradiction and contested status. Valence must be a property of evidence-to-claim attachment, separate from the source event’s sentiment/action.

**Primary code:** [belief_engine/pipeline/steps/update_beliefs.py:62](../../belief_engine/pipeline/steps/update_beliefs.py#L62).

### MEM4 — P2: Reprocessing old evidence manufactures fresh observation metadata

Evidence insertion deduplicates unchanged observations, but every upsert still increments observation_count and sets last_confirmed from the request. The nightly updater supplies today, including no_change with no new evidence. Live retrieval ranks by those fields. In addition, rolling ticket aggregates with overlapping constituent events produce new summaries and therefore new weighted evidence rows as the count/window changes.

**Evidence and direction:** Reproduced one evidence row but two observations and a new confirmation date. Exact duplicate evidence weight is protected; recency/frequency are not. Aggregate overlap is confirmed structurally, not measured in live data. Count new source observations and retain stable source-event identities; do not sum overlapping rollups as independent observations.

**Primary code:** [belief_engine/store/belief_store.py:316](../../belief_engine/store/belief_store.py#L316).

### MEM5 — P1: Owner corrections and suppression are not enforced at the write boundary

The central upsert has no locked guard. The updater checks locks only while action is update/deprecate, then later converts create on an existing key into update. That path overwrites a locked statement. Other callers can also upsert it. Suppression deprecates a row; the archive later removes it from the live table, and creation does not consult an enduring suppression record or archive. Old evidence can recreate the same key under a new id.

**Evidence and direction:** Reproduced both the actual updater create-path lock bypass and archive/same-source recreation in temporary databases. Enforce owner authority transactionally in the shared writer and preserve suppression/correction tombstones independently of active rows.

**Primary code:** [belief_engine/store/belief_store.py:322](../../belief_engine/store/belief_store.py#L322).

### MEM6 — P1: A belief can commit before its evidence, and an embedding failure interrupts the evidence write

upsert_belief commits the claim, performs the Chroma upsert, then inserts evidence in separate transactions. A failed embedding call leaves a committed active claim with no newly attached evidence. Recompute explicitly skips beliefs with no usable evidence, so it cannot subsequently age such a row out through this model. Empty/invalid evidence refs on create and reevaluator-created split rows are additional evidence-free paths.

**Evidence and direction:** Injected an embedding failure: the method raises, but an active row remains with no evidence. Commit claim/version and evidence atomically, then process a durable vector-sync job. Reject unsupported inference writes or represent their provenance/derivation explicitly.

**Primary code:** [belief_engine/store/belief_store.py:329](../../belief_engine/store/belief_store.py#L329).

### MEM7 — P1: Contested beliefs can fall out of the reevaluation work queue

Reevaluation consumes only keys on the current run context. Recompute scans active rows and reloads persisted contested keys only when that run reports a new contested flip. Write-time dedup can mark another stored belief contested without adding its key to that context. Canonicalization can contest both sides after the reevaluator has already run. Failed reevaluations also lack an unconditional persisted-queue drain. Active-only retrieval then hides these beliefs while they wait.

**Evidence and direction:** Reproduced a persisted contested belief with no fresh flip: the next recompute/reevaluate sequence reports no contested beliefs and leaves it stranded. Drain persisted contested work independently of whether new conflicts were discovered, with bounded retries and visible failure state.

**Primary code:** [belief_engine/pipeline/steps/reevaluate_beliefs.py:72](../../belief_engine/pipeline/steps/reevaluate_beliefs.py#L72).

### MEM8 — P1: The main KG question-answering payload omits structured validity dates

Both global and node-anchored ask_kg build node evidence containing label/type/description/aliases/optional attributes, omitting first-class start_date, end_date and observation fields. Edge evidence includes updated_at, a maintenance/write time rather than validity. The RAG agent is told to answer solely from that payload. Dates might occur incidentally in prose, but the authoritative temporal distinction can be unavailable. This finding concerns handle_ask_kg, not every KG reader: shared card/wiki bullets do render temporal information.

**Evidence and direction:** Confirmed by both payload builders and the shared ask_kg prompt; no live wrong answer claimed. Include structured validity, observation time, confidence and closure reason in every selected claim, and teach current-versus-historical interpretation.

**Primary code:** [app/assistant/lib/core_tools/kg_search/knowledge_graph_search.py:509](../../app/assistant/lib/core_tools/kg_search/knowledge_graph_search.py#L509).

### MEM9 — P1: One late re-observation can prevent KG state decay indefinitely

The expiry anchor is start_date/first_observed/created_at plus the estimated duration. A last_observed timestamp at or after that fixed expected_end causes an unconditional skip. As time advances the same old timestamp continues satisfying the test. Re-observation does not establish a new expiry deadline here.

**Evidence and direction:** Reproduced a three-day state observed on day four and otherwise silent for roughly 296 days: it is still skipped as recent activity. Define renewed freshness relative to the latest genuine observation, without equating silence-based expiry with proof that an event actually ended.

**Primary code:** [app/assistant/pipelines/kg_maintenance_pipeline/step_state_decay.py:148](../../app/assistant/pipelines/kg_maintenance_pipeline/step_state_decay.py#L148).

### MEM10 — P2: A transient KG extraction failure is persisted outside the retry queue

The pending-window query excludes every window with any extraction row. An extractor exception is caught and persisted as verdict=error, then counted as processed. The next run excludes that window, and the generic runner does not see the exception. Thus a transient model/provider error can permanently omit that window from automatic extraction until explicit repair.

**Evidence and direction:** Confirmed by exception, selection and pending-count paths; no automatic error requeue found in the reviewed live pipeline/routes. Retain failed work as retryable with bounded backoff, attempt count and an operator-visible dead-letter state.

**Primary code:** [app/assistant/pipelines/kg_pipeline/steps/critique_and_extract.py:45](../../app/assistant/pipelines/kg_pipeline/steps/critique_and_extract.py#L45).

### MEM11 — P2: Qualifiers and confidence/decay metadata have inconsistent contracts

An update that omits conditions clears the stored conditions; no_change constructs such a request. The live retrieval API reads extraction-time confidence while the export prefers current_confidence_band. The evidence half_life_days_snapshot is stored with comments promising historical preservation, but recompute does not select it and instead applies the current belief kind to all evidence.

**Evidence and direction:** Qualifier erasure reproduced; confidence and half-life inconsistencies confirmed by reads. Preserve omitted qualifiers, expose a named effective-confidence contract, and decide explicitly whether reclassification recomputes historical decay or honors evidence snapshots.

**Primary code:** [belief_engine/store/belief_store.py:315](../../belief_engine/store/belief_store.py#L315).

### MEM12 — P2: Write-time dedup lacks the incoming evidence dates and can admit known-stale beliefs as active

The merge verifier gets the stored belief’s dated trail but only a generic new-statement context for the incoming side. The actual incoming evidence is resolved later. If the verifier nevertheless says the stored side is current, the code deliberately proceeds to write the incoming stale side, ordinarily active, relying on a future sweep. Agent readers can see both meanwhile.

**Evidence and direction:** Confirmed by dedup input construction and the supersedes/current_side branch. Pass both source-dated trails before deciding; retain historical claims as historical, not competing current guidance. This is separate from ordinary semantic near-match uncertainty.

**Primary code:** [belief_engine/pipeline/steps/update_beliefs.py:151](../../belief_engine/pipeline/steps/update_beliefs.py#L151).

### MEM13 — P2: Belief pipeline success can conceal per-belief failures

The pipeline marks a step successful whenever it returns without raising. UpdateBeliefsStep catches per-belief errors and returns partial_error; recompute and reevaluation also carry error counters. These do not change the pipeline’s overall success, so the adapter can report success and export a partially updated set.

**Evidence and direction:** Confirmed by step and adapter contracts. Preserve partial completion but propagate partial/error health, count outstanding repairs and avoid calling the entire run successful.

**Primary code:** [belief_engine/pipeline/pipeline.py:108](../../belief_engine/pipeline/pipeline.py#L108).

## Design concerns, distinct from confirmed defects

### 1. Define the division by meaning, not storage duration

Use the KG for source-grounded identities, factual relationships, events and their
validity. Use beliefs for revisable interpretations and action guidance derived
from that evidence: likely preferences, habits, inferred constraints, confidence
and exceptions. Working context belongs in the current conversation/task or another
explicitly expiring state view. An appointment is an event; “usually prefers morning
appointments” is a belief; “busy for the next 20 minutes” is current context.

This need not prohibit every overlap. It requires links and an explicit precedence
rule when views disagree. Today belief keys/statements do not provide canonical KG
subject/claim foreign keys, and a belief may refer to several household members only
in prose. A general cross-store contradiction/correction protocol was not found.

### 2. A confidence label must not masquerade as a probability

The belief score is a heuristic sum of decayed weights against thresholds. It does
not establish independent observations, source accuracy, or a calibrated probability.
Insight text, ticket aggregates and feedback can describe the same underlying event.
Keep source reliability, evidence independence, inference strength and freshness
separate enough that an agent can understand uncertainty.

### 3. Distinguish stale from false, and inferred expiry from an observed end

Silence can make a current-state claim unsafe to use; it does not prove it became
false on an estimated date. Likewise a user who stops discussing a preference has
not necessarily changed it. KG auto_decay already labels its end-date confidence,
but every reader must preserve that distinction. Belief deprecation and physical
archive should retain why a claim was retired and whether future evidence may revive it.

### 4. Give every reader a shared memory contract

A useful claim projection should carry: subject IDs, claim/version ID, text, explicit
versus inferred origin, source references, observation time, validity/freshness,
status, effective confidence, conditions, user authority and supersession links.
Provide related contradictions/corrections when relevant, not just the active winner.
The existing batch-wide top-8 similarity lookup is weak coverage for a diverse
14-day evidence bundle. Per-topic/entity retrieval and contradiction candidates
would be safer than assuming one embedding query represents every topic.

### 5. Make health visible without repeatedly bothering the user

Track unprocessed source windows, pending memory deliveries, evidence-free claims,
contested backlog, stale projections and vector-sync failures. Use bounded repair
queues. Keep operational repair separate from re-asking the user about an already
settled preference. User-owned decisions should survive retries, archival and wording changes.

## Additional discrepancies and follow-up checks

- Several instructions are still assembled in Python: the belief updater/reevaluator
  `agent_input.task` strings, ask_kg task text, and a larger context-activation prompt
  block. This conflicts with the established Jinja-only prompt policy. Data formatting
  can remain Python; behavioral instructions should move to the existing templates.
- Belief documentation says incoming write-time dedup has dated evidence on both
  sides; the incoming side does not. Schema comments say readers use snapshot confidence;
  live retrieval reads extraction confidence. Half-life snapshot comments promise a
  behavior recompute does not implement. The routine JSON still describes the retired
  per-domain loop. These are documentation findings, not reasons to preserve broken code.
- KG pipeline documentation calls the promoter the sole live-KG writer, although
  explicit mutator tools and maintenance also write it. Its decay summary promises
  re-observation reopening; the reviewed refresh helper fills only missing validity
  fields and does not generally clear an existing end date. A separate reopening policy
  and all alternate write paths need focused verification before claiming that guarantee.
- KG runner `_is_database_locked` treats every sqlite3.OperationalError as a lock
  retry, including errors that need repair rather than waiting. This is a confirmed
  overly broad classification; its runtime frequency and all driver exception wrappers
  were not measured here.
- Evidence identity is checked then inserted in separate sessions with no matching
  unique constraint in the reviewed belief ORM. Concurrent duplicate attachment is a
  reachable race by inspection, not reproduced under concurrency in this audit.
- The two stores and their materialized projections can lag each other. Owner UI
  changes, tagging/export schedules and every derived resource invalidation path
  deserve dedicated consistency tests; no universal instantaneous visibility claim
  is justified by the paths reviewed here.

## Recommended repair order

1. **Protect user meaning and authority:** MEM1–3 and MEM5. Wire noticer delivery,
   preserve response semantics, use claim-relative evidence polarity, and enforce
   locks/suppression centrally. These directly affect whether the assistant learns the opposite
   of what the user intended or ignores a correction.
2. **Make currentness trustworthy:** MEM8–9 plus qualifier preservation in MEM11.
   Deliver temporal facts to readers and fix the stale-state exemption.
3. **Make memory writes/recovery reliable:** MEM6–7, MEM10 and MEM13. Atomic claim+
   evidence writes, durable vector sync, durable reevaluation and extraction recovery,
   and honest run status.
4. **Fix evidence accounting and reconciliation:** MEM4, MEM12 and the remaining
   MEM11 contract mismatches. Stable observation identity, source-dated comparisons
   and one effective-confidence definition.
5. **Clarify the shared design:** link claims across stores, standardize projections,
   and add end-to-end memory scenarios before expanding producers or adding agents.

Do not start with a new memory agent or a third store. Most confirmed problems are
in deterministic persistence, scheduling, evidence interpretation and projection.

## Validation and limits

Ten diagnostic probes exercised the current code with temporary databases/files,
fake embeddings and fake model outputs. All ten reproduced the asserted current
behavior: stale observation counters/condition loss, shared-writer lock overwrite,
embedding-failure partial write, negative-preference polarity inversion, dropped
noticer update, stranded contested work, permanently exempt KG state, expiry-as-snooze,
the actual updater create-path lock bypass, and archive/same-source recreation.
These are reproductions of defects, not ten passing correctness regressions.

Probe source is retained in the task workspace under `memory-audit/test_audit_probes.py`.
The remaining confirmed findings are traced code paths, with that distinction noted.
No production code, live beliefs, KG rows or prompts were changed; no live model
quality evaluation or historical corruption census was performed.

Coverage includes the active belief collector/updater/store/decay/reevaluator/
canonicalizer/export/retrieval/archive and owner/feedback/noticer paths; KG extraction,
promotion/re-observation/conflict sections, decay, ask_kg payloads, shared card/wiki
projection structures, and context-engine integration. It is not an exhaustive audit
of every KG maintenance agent, every mutator operation, all concurrency races, or
model accuracy. Dedicated live-read and controlled end-to-end tests should follow
the deterministic repairs.


## Follow-up: existing foundations and actual insights flow

The existing design should be repaired incrementally. It already separates source
insights, belief updates, deterministic evidence decay, semantic reevaluation,
canonicalization and planning projection. This is substantial reusable architecture.

Actual wired flow:

- Daily context snapshot + tickets + master-room user messages -> merged timeline ->
  daily_timeline_insights -> dated resource_daily_insights.json.
- Daily assessment -> daily assessment summary -> weekly_synthesizer over seven daily
  summaries -> weekly resource including belief_candidates.
- Active v1 belief collector reads fourteen days of daily insights and ticket
  aggregates -> updater -> evidence-weight recompute -> contested reevaluator ->
  canonicalizer -> inline JSON export after reported pipeline success.
- Dayflow routine writer reads selected exported beliefs plus weekly patterns and
  the first three weekly candidates directly -> resource_dayflow_routine ->
  orchestrator evaluator, work architect and state mover prompts.

Weekly candidates do not currently feed the active belief collector. This differs
from the intended daily + weekly evidence -> beliefs architecture. Their direct
routine projection also omits candidate conditions, confidence and evidence. See MEM15.

Foundations worth keeping:

1. Daily insight prompt requires user-authored evidence, avoids durable inference
   from acceptance alone, distinguishes today-only facts from enduring patterns.
2. Weekly synthesis targets cross-day patterns, requires three days or repeated
   explicit statements, and carries qualifiers/evidence in its output schema.
3. Belief updater aims for independently changeable statements, existing-key reuse,
   exact timing preservation, conditions, and explicit preference replacement.
4. Evidence math keeps separate support and contradiction weights. Current half-lives
   are durable facts: none; relationships: 1825 days; preferences: 365; routines: 90;
   episodic context: 14; transient states: 1. These are evidence-weight half-lives,
   not automatic deletion deadlines or calibrated truth probabilities.
5. Reevaluation can confirm, rewrite, qualify, split or deprecate. Its prompt already
   asks for baseline-plus-exception reasoning and permits unresolved contestation.
   The evidence input is bounded to the latest 50 records despite full-history wording.
6. Semantic deduplication and periodic canonicalization already exist. Owner locks,
   evidence storage and exported confidence also exist, with enforcement/accounting
   defects identified above rather than a need to invent all these facilities.

The main work is faithful evidence semantics and lineage, connecting weekly synthesis,
reliable contested-work processing, enforcing owner corrections, and preserving
provenance/uncertainty into planning. Routine projection currently reduces a belief
to [domain/confidence] statement [conditions]; it drops IDs and freshness metadata.
The routine writer then transforms those into planning prose. Preserve this useful
planning stage while strengthening the context contract.

The structured extracted_claim fields emitted upstream belong to a v2 design whose
primary flag is false; the active v1 collector does not consume them. Its decay rules
therefore differ from the upstream prompt's promises. See MEM14 follow-up.

This follow-up is code inspection, not verification of recent live routine execution.
Tracked routine defaults are daily insights 00:05, weekly Monday 00:10 and beliefs
00:30 local; private routine overrides were not inspected. No runtime code changed.
