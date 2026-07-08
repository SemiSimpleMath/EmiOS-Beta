# Routine inventory

What every tracked routine in `configs/routines/public/` actually does. Assembled from the `notes` field of each routine file — the canonical description lives in the JSON, this doc is just the categorized view. Trigger types, policies, active windows, and on_error backoff are documented in `skills/extending-emi-routines/SKILL.md`; the manager mechanics live in `docs/architecture/06_PIPELINES_AND_ROUTINES.md`.

**Total: 56 public routines** — 51 enabled, 5 disabled (`belief_engine_export`, `example_camera_motion_poll`, `morning_briefing`, `sample_morning_routine`, `timesheet_routine`). The v2-store `belief_tag_new` was deleted 2026-07-07. The `morning_briefing.compiled.json` sibling is compiled task IR, not a routine.

Personal routines live in `configs/routines/private/` (gitignored) and override matching public ids on collision — see `skills/extending-emi-routines/SKILL.md` for the layout.

## KG ingest (chat to live KG)

The always-on backbone — turning what you say into a knowledge graph.

### `kg_pipeline`

**Cadence:** daily 23:00, active window `kg_active` (pipeline runner; cadence in `trigger.policy`)

Ingests chat messages into the knowledge graph (2026-04-25 refactor — bucket-per-stage architecture). Runs once per day at 23:00 inside the kg_active window (configs/windows.json). Tune the window itself, not the time_local — the window is the active period; time_local just says 'fire at the start of it.'

### `proposal_promoter`

**Cadence:** daily 02:30 (function)

Nightly sweep over pending claim_proposals. Auto-promotes anything resolvable + conflict-free. Held proposals (conflicts, lock violations, placeholders) stay in the review queue. Manual trigger: POST /kg-proposals/run-promoter (dry-run) or ?commit=1.

### `chat_memory_index`

**Cadence:** every 3600s (function)

Indexes recent chat summaries and user messages into ChromaDB for conversational memory RAG. Summaries indexed over 90 days, user messages over 14 days.

### `camera_dispatch`

**Cadence:** event-triggered on `ring_snapshot_captured` (function)

Handles every ring_snapshot_captured event. Routes by camera_id to the per-camera analyzer + storage + pod policy declared in configs/cameras.json. Replaces the bespoke register_camera_dispatcher() startup hook with a declarative event-triggered routine.

## KG maintenance & investigation

Keeps the graph healthy: detects issues (duplicates, missing dates, stale states), investigates them, and either auto-applies fixes or surfaces questions to you. All function runners unless noted.

### `kg_maintenance_pipeline`

**Cadence:** weekly Monday 02:00 (pipeline)

Weekly KG maintenance scan: orphan nodes, missing descriptions, suspect nodes (LLM), duplicate pairs (Tier 4 anchor-propagation + LLM random sampling). All findings written to kg_maintenance_finding for review at /kg-maintenance.

### `kg_finding_backlog_drain`

**Cadence:** daily 03:00

Daily: investigate up to 20 oldest pending kg_maintenance_findings (drain by node importance), EXCLUDING duplicate_node (goes through kg_dup_cluster_drain), state_missing_dates (dedicated kg_state_date_drain), and synthetic_fact_proposal (own review inbox). Complements the weekly kg_maintenance_pipeline which only investigates findings from its own run.

### `kg_finding_executor_drain`

**Cadence:** every 900s (15 min)

Drains the executor bucket every 15 min: investigated findings whose 24h dev-page grace window expired AND user-Accepted findings. Filters to disposition='auto_apply'; 'needs_user_review' stays in the user queue. Hands each to kg_resolution_manager which applies the matching mutations from its full mutator suite. Closes the self-healing loop: pending -> investigated -> executed | escalated | dismissed. Instant drain: POST /api/routines/kg_finding_executor_drain/run-now.

### `kg_finding_cluster_resolve`

**Cadence:** daily 03:30

Reads pending findings, groups by primary_node_id, calls kg_finding_cluster_resolver to verify which candidate clusters share ONE root question. Confirmed leads get a synthesized root_question; siblings get superseded_by stamped (hidden from default UI). Resolving a lead cascades to its siblings. Runs after wiki_nightly_refresh (03:00) so same-night wiki_contradiction findings are eligible.

### `kg_dup_cluster_drain`

**Cadence:** daily 03:15

The canonical duplicate_node resolver. Union-finds pending duplicate_node findings into connected components and adjudicates each in ONE kg_maintenance::duplicate_cluster_resolver (gpt-5.4) call: merge true label/coreference variants, keep recurring-event occurrences DISTINCT. Distinct verdicts go to the durable verdict store (scanner short-circuit); true dups merge via the revertible node_merge path (kg_merge_log). limit=50 clusters/run; dry_run:true + run-now writes scratch/dup_cluster_report.json without mutating.

### `kg_state_date_drain`

**Cadence:** daily 03:15

Drains pending state_missing_dates findings through kg_investigation_manager with the specialized state-dates brief (dated neighbors, conversation-evidence window, sibling states). Investigator emits proposed_action.op='fill_dates' with confidence; high-confidence ones get auto-applied by kg_finding_executor_drain. Limit 8/day keeps LLM cost bounded.

### `kg_date_gap_drain`

**Cadence:** daily 09:00

Daily 09:00: pick the 2 highest-priority state_missing_dates findings (7-day re-ask cooldown per finding) and enqueue each as a pending question (topical_tag=kg_date_gap). The conversation-starter bridge surfaces them in persona under its own daily nudge budget; /kg-maintenance/date-gaps stays the pull-based review surface.

### `kg_wiki_inference`

**Cadence:** daily 04:15

Reads the 10 freshest wiki pages and runs wiki_connection_investigator to propose edges (and sometimes new target nodes) the page implies but the KG doesn't yet have. Proposals flow through the standard claim_proposal pipeline so the promoter's merge gates apply. Runs after wiki_nightly_refresh and the cluster/executor drains so it sees the freshest pages.

### `kg_state_decay`

**Cadence:** daily 02:45

Auto-closes State/Event nodes whose TTL has elapsed (attributes.last_observed + attributes.ttl, GRACE_DAYS=3). Runs after proposal_promoter (02:30) so freshly-promoted nodes are in the candidate set, and before wiki_nightly_refresh (03:00) so closures flow into the wiki the same night.

### `kg_goal_outcome_detect`

**Cadence:** daily 02:55

Scans recent chat for explicit 'I did X' / 'I gave up X' statements about active Goal nodes. Closes matching Goals as terminal (achieved/abandoned) with end_date set. Conservative — only fires on direct user statements at confidence >= 0.7. Silent abandonment is goal_dormancy_sweep's job. Limit 10 Goals/day.

### `kg_goal_dormancy_sweep`

**Cadence:** daily 02:50

Flips long-silent Goal nodes from active -> dormant. Reversible — any re-observation in the promoter flips back to active. NEVER sets end_date. Threshold = max(ttl.estimated_duration_days * 1.5, 30) days, capped at 730d; default 90d when no TTL. Lifelong-value Goals are skipped. Runs alongside state_decay (02:45) so downstream card/wiki refreshes see consistent goal_status.

### `kg_identity_sentence_refresh`

**Cadence:** daily 03:45

(Re)generates identity sentences — graph-derived definite descriptions that uniquely identify each node's referent. NULL-sentence backfill first by pagerank, then hash-stale nodes (label/era/anchor-edge drift). Embeds into node_identity_embeddings; resolution + merge judges read them. Runs after the KG drains, before card refresh (04:30). max_per_run 1500 (free local models; server is the only chroma writer).

### `kg_evidence_digestion`

**Cadence:** daily 04:00

Folds the oldest evidence rows of the heaviest-evidenced nodes (>=40 live rows) into ONE consolidated digest row each, keeping the newest 15 verbatim. NOTHING DELETED: originals stamped digested_at and kept for provenance. Max 80 rows/node/night, 5 nodes/night, one gpt-5.4-mini call/node. Runs after identity refresh (03:45).

### `kg_embedding_diff_sync`

**Cadence:** every 3600s (hourly)

Reconciles the chroma label + identity + context collections against sqlite: removes ghost vectors (deleted/merged nodes), embeds missing ones, re-embeds text drift (rename / identity regen). Local MiniLM = free; no-op when in sync. Bounded max_embeds=1200/run so a large backlog converges over runs; max_run_seconds 900. Drift on the resolution path cannot survive an hour.

### `kg_embedding_full_rebuild`

**Cadence:** weekly Sunday 04:10

Re-embeds EVERY node's label + identity + context vector regardless of apparent freshness (~20k embeds, 2h+ at this machine's ~0.4s/embed; 3h budget). Belt-and-suspenders floor plus the tool for embedding-model migrations. Demoted nightly->weekly after a watchdog breach.

### `kg_importance_rater`

**Cadence:** every 1800s (30 min)

ONE LLM rater (me::edge_importance_rater on unrated edges), then all node importance is DERIVED: Entity/Concept = max(adjacent edge importance); State/Event/Goal/Property = max(src_imp × edge_imp / 10). Replaces the buggy me::importance_rater Entity-LLM path. Cheap, idempotent; also section-tags untagged nodes (max_edges_per_run 400 cap + watchdog).

## Entity cards & wiki

Generates and refreshes the human-readable views on top of the KG.

### `entity_card_refresh`

**Cadence:** daily 04:30 (function)

Nightly 04:30 (after the KG drains): rebuild up to 10 stale cards (entity or neighbor updated since last build, per-card 7-day cooldown so cards never churn), build up to 5 newly card-worthy entities (post-build critic veto kills noise L0s), deactivate orphaned cards. Wires the entity_cards_v2 refresh loop that the v2 design never connected.

### `wiki_nightly_refresh`

**Cadence:** daily 03:00 (function)

Scans the vault, regenerates only sections affected by KG node/edge updates since each page's generated_at, then runs the consistency critic. Runs after proposal_promoter (02:30) so newly-promoted nodes flow into the wiki the same night.

### `wiki_growth`

**Cadence:** daily 03:30 (function)

Builds up to max_new_pages new prose pages each night for the highest-degree Entity nodes that don't yet have one. Runs after wiki_nightly_refresh. Pairs with refresh: refresh maintains existing pages, growth adds new ones. Tune max_new_pages to control daily LLM spend.

### `wiki_fact_drain`

**Cadence:** every 86400s (daily), active window 08:00–22:00 (function)

Drains synthetic_fact_proposal findings (wiki_connection_investigator's 04:15 output): trivia gets dismissed by the worthiness gate; worthy facts become natural confirmation questions (max 2/run, riding the standard question + answer-capture loop). Confirmed facts become investigated/auto_apply findings the executor materializes after its 24h grace. Window starts 08:00 so morning noticer/digest questions get priority.

## Beliefs

The belief store lifecycle (v1 belief_engine). Domains are toggled in `configs/belief_domains.yaml`.

### `belief_engine`

**Cadence:** daily 00:30 (pipeline)

Loops over every domain marked enabled in configs/belief_domains.yaml and updates that domain's beliefs from the last 14 days of daily insights + ticket signals. Adding/disabling a domain is a YAML edit, not code. Domain failures are logged but don't abort the rest. After all domains succeed, the adapter exports active beliefs to resource_user_beliefs.json inline.

### `belief_engine_export` *(disabled)*

**Cadence:** daily 01:00 (pipeline)

DISABLED for scheduled execution — belief_engine now exports inline at the end of its run. Pipeline registration kept so an operator can /run-routine belief_engine_export for a manual re-export. Content-guarded — file only rewrites if beliefs changed.

### `belief_archive`

**Cadence:** daily 05:30 (function)

After the v1 belief_engine rebuild (00:30, which deprecates faded + merged beliefs) and the feedback_extractor drain (04:00–05:00). The lifecycle terminus: evicts deprecated beliefs (+ their evidence) from the live user_beliefs/belief_evidence into the *_archive tables so the live store stays lean. Idempotent. belief_short_id + belief_merges stay live (counter integrity + provenance).

### `belief_tag_v1`

**Cadence:** daily 05:35 (function)

After the v1 belief_engine rebuild (00:30). Tags active user_beliefs that are untagged OR stale (statement changed since last tagged), from the standardized vocab (configs/belief_tags.yaml). 'needs' selector + max_per_run cap + max_run_seconds watchdog; on_error self-heals transient failures.

### `feedback_extractor_daily`

**Cadence:** every 86400s (daily), active window 04:00–05:00 (function)

Daily drain of unprocessed feedback.comment pods into the belief store. Fires once in the 04:00–05:00 local window so the morning's belief snapshot reflects yesterday's user comments. Reads queue via list_recent_unprocessed_comments; LLM extracts belief updates; BeliefStore.upsert_belief writes them; comment pods marked processed_at_utc. Fast-paths out when the queue is empty. on_error.auto_retry_after_seconds=3600 self-heals transient failures.

## Subconscious

The proactive mind: daily proposers mint intention pods, an arbiter schedules them, plus the noticer / digest / feedback loops. All function runners. (See the subconscious overhaul memory for the surface.)

### `subconscious_grocery_sync`

**Cadence:** every 86400s (daily), active window 04:30–05:00

Daily: scans recent user chat for grocery intents (bought / ran out / consumed), applies them to the inventory, runs decay. Runs in the 04:30–05:00 window so inventory_snapshot is fresh for the 05:00 daily_meal_proposer. CLI: python -m app.assistant.subconscious.run_grocery_sync.

### `subconscious_daily_meal_proposer`

**Cadence:** every 86400s (daily), active window 05:00–05:30

Reads today's meal-addressable concerns + grocery inventory + recent feedback. Mints intention.meal + intention.shopping pods. The scheduler_arbiter (05:30) decides which intentions land on the calendar.

### `subconscious_wellness_proposer`

**Cadence:** every 86400s (daily), active window 05:00–05:30

Whole-domain wellness (workouts + sleep + recovery + hydration + meditation), NOT just fitness. Reads wellness-addressable concerns + sleep/activity signals. Mints intention.wellness pods. Acute medical escalates through the noticer to dayflow tickets, not here.

### `subconscious_romantic_proposer`

**Cadence:** every 86400s (daily), active window 05:00–05:30

Relationship-domain proposer. Reads key_dates (anniversaries, birthdays), relationship signals, recent calendar density. Tone-calibrated — explicit skip-with-reason preferred over forcing low-quality proposals. Mints intention.romantic pods.

### `subconscious_scheduler_arbiter`

**Cadence:** every 86400s (daily), active window 05:30–06:00

Runs AFTER the three proposers. Reads intention.* pods minted today + household_calendar (hard constraint) and emits ONE plan.weekly_schedule pod with is_anchor flags. Hard conflicts surface as dayflow tickets; deduplicates near-identical proposals from different proposers.

### `subconscious_weekly_meal_planner`

**Cadence:** weekly Sunday 17:00

Sets the next week's meal plan on Sunday evening. The daily_meal_proposer adjusts day-by-day; this run sets the strategic shape (Mon–Sun mix, shopping anchor list). Output: plan.weekly_meals pod + shopping list. Writes a meal-doc snapshot to resources/subconscious/.

### `subconscious_skill_distiller`

**Cadence:** weekly Sunday 22:00

Reviews the week's intention.* pods + arbiter decisions + chat outcomes; PROPOSES rule additions to canonical task_spec files. No auto-apply — proposals land in resources/subconscious/resource_learned_skills_proposed.md for manual review. Skipped reasons are explicit so restraint is auditable.

### `subconscious_meal_feedback`

**Cadence:** every 3600s (hourly), active window 07:00–21:00

PRODUCE: enqueue 'How was <dish>?' pending_questions for recent past meals (the conversation_starter bridge asks them). INGEST: turn the user's chat reply into a feedback.comment pod -> feedback_extractor -> beliefs. Closes the ask->answer->belief loop. CLI: python -m app.assistant.subconscious.run_meal_feedback.

### `subconscious_noticer`

**Cadence:** every 86400s (daily), active window 04:00–22:00

Single daily noticer pass (Pass A inward pattern detection + Pass B outward opportunity scouting). The wide 04:00–22:00 window means it runs at ~04:00 when the machine is on (concerns ready before the 05:00 proposers) but falls back to the first waking-hours tick if the machine was off overnight, so it no longer silently skips days. Output: concerns_register + belief_updates + pending_questions + escalations to dayflow.

### `subconscious_answer_sweep`

**Cadence:** every 3600s (hourly), active window 07:00–22:00

Hourly sweeper behind the per-turn answer check: judges asked-but-unanswered noticer questions against the user chat that followed (answer_matcher, cheap model), marks captured answers, annotates the related concern, triggers an immediate noticer tick. No open questions = pure SQL no-op.

### `subconscious_digest`

**Cadence:** every 86400s (daily), active window 07:30–22:00

Daily digest of the concerns_register posted to master_room (new vs ongoing vs recently-resolved concerns + pending questions). Pure templating, no LLM. Window starts 07:30 so the 04:00 noticer tick and 05:00–06:00 proposers have run; the wide fall-back window means a late boot still gets its digest. Also lands in app/subconscious_digests/ and on /subconscious.

## Fetching external data

Pull-ins from your other accounts. Each is feature-gated — disabled until you connect the relevant integration in Settings. All `tool` runners.

### `fetch_email`

**Cadence:** every 300s — feature `email`

Periodic email fetch across all active Gmail OAuth accounts. Replaces BackgroundTaskManager round-robin data_fetch.

### `fetch_calendar_events`

**Cadence:** every 1620s — feature `calendar`

Periodic calendar sync. Defaults to local midnight -> +7 days when no dates provided.

### `fetch_todo_tasks`

**Cadence:** every 1860s — feature `tasks`

Periodic Google Tasks sync.

### `fetch_weather`

**Cadence:** every 660s — feature `weather`

Periodic weather update. City defaults from resource_current_location.json.

### `fetch_news`

**Cadence:** every 1380s — feature `news`

Periodic news fetch from configured RSS feeds. Empty feed_urls falls back to DEFAULT_FEEDS.

### `fetch_scheduler_events`

**Cadence:** every 180s — feature `scheduler`

Periodic internal scheduler event sync. Defaults to midnight -> +7 days.

### `location_refresh`

**Cadence:** every 900s (function)

Rebuilds user location timeline from calendar events and updates resource_current_location.json. Used by weather tool and location_summary context.

### `situation_audit`

**Cadence:** every 1800s (function)

Periodic background audit of all active context. Checks for contradictions, conflicts, and anomalies across data sources. Notifies user of findings via chat. Passive observer only — does not modify plans or tasks.

## Dayflow & insights

The daily orchestration layer + summary rollups.

### `dayflow_pipeline`

**Cadence:** every 60s (pipeline)

Step-based DayFlow pipeline.

### `daily_insights_pipeline`

**Cadence:** daily 00:05 (pipeline)

Nightly pipeline: archive context + tickets, build timeline, extract insights, apply to resource files, build assessment + summary. Runs steps sequentially in-process.

### `weekly_insights_pipeline`

**Cadence:** weekly Monday 00:10 (pipeline)

Reads the 7 most recent daily assessment summaries and synthesizes cross-day patterns and belief candidates.

## Housekeeping

### `sandbox_outputs_sweep`

**Cadence:** daily 03:30 (function)

Drops files under data/sandbox_outputs/<call_id>/ older than max_age_days, then removes empty call_id subdirs. The on-disk files back metadata.stored_path on binary execute_code output pods so downstream tools (send_email, pod_fetch) can attach the real bytes. Pods themselves are kept in pod_store regardless; only the file backing eventually expires.

## Disabled examples / templates

Disabled out of the box. Copy to `configs/routines/private/<id>.json`, customize, and flip `enabled` to use.

### `morning_briefing` *(disabled)*

**Cadence:** daily 05:30 (task)

Compiled task: playwright headlines, emails, todos, daily summary agent, save to storage.

### `sample_morning_routine` *(disabled)*

**Cadence:** daily 07:00 (task)

Example routine. Enable and adjust time to use.

### `timesheet_routine` *(disabled)*

**Cadence:** daily 09:00 (task)

Manual/on-demand timesheet narrative routine. Scheduled execution remains disabled by default.

### `example_camera_motion_poll` *(disabled)*

**Cadence:** every 60s (function)

Polls Ring's recent-events history for one camera. On any new motion/ding event since the watermark, captures a fresh snapshot via the Ring bridge -> publishes ring_snapshot_captured -> camera_dispatch routes to the analyzer + skill configured for this camera in configs/cameras.json.
