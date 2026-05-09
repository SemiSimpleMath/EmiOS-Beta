# Routine inventory

What every routine in `configs/routines/public/` actually does. Assembled from the `notes` field of each routine file — the canonical description lives in the JSON, this doc is just the categorized view.

**Total: 36 public routines** (35 active + 1 disabled example).

Personal routines live in `configs/routines/private/` (gitignored) and override matching public ids — see `skills/extending-emi-routines/SKILL.md` for the layout.

## KG ingest (chat to live KG)

The always-on backbone — turning what you say into a knowledge graph.

### `kg_pipeline`

**Cadence:** daily at 23:00, active window `kg_active`

Ingests chat messages into the knowledge graph (2026-04-25 refactor — bucket-per-stage architecture). Runs once per day at 23:00 inside the kg_active window (configs/windows.json). Tune the window itself, not the time_local — the window is the active period; time_local just says 'fire at the start of it.'

### `proposal_promoter`

**Cadence:** daily at 02:30

Nightly sweep over pending claim_proposals. Auto-promotes anything resolvable + conflict-free. Held proposals (conflicts, lock violations, placeholders) stay in the review queue. Manual trigger: POST /kg-proposals/run-promoter (dry-run) or ?commit=1.

### `chat_memory_index`

**Cadence:** every 3600s

Indexes recent chat summaries and user messages into ChromaDB for conversational memory RAG. Summaries indexed over 90 days, user messages over 14 days.

### `camera_dispatch`

**Cadence:** event-triggered on `ring_snapshot_captured`

Handles every ring_snapshot_captured event. Routes by camera_id to the per-camera analyzer + storage + pod policy declared in configs/cameras.json. Replaces the bespoke register_camera_dispatcher() startup hook with a declarative event-triggered routine.

## KG maintenance & investigation

Keeps the graph healthy: detects issues (duplicates, missing dates, stale states), investigates them, and either auto-applies fixes or surfaces questions to you.

### `kg_maintenance_pipeline`

**Cadence:** weekly Monday at 02:00

Weekly KG maintenance scan: orphan nodes, missing descriptions, suspect nodes (LLM), duplicate pairs (LLM random sampling). All findings are written to kg_maintenance_finding table for review at /kg-maintenance.

### `kg_finding_backlog_drain`

**Cadence:** daily at 03:00

Daily: investigate up to 5 oldest pending kg_maintenance_findings (FIFO drain). Picks from the global pending queue, complementing the weekly kg_maintenance_pipeline which only investigates findings produced by its own run.

### `kg_finding_executor_drain`

**Cadence:** daily at 03:45

Drains investigated findings whose 24h dev-page grace window has expired. Filters to disposition='auto_apply' (set by kg_investigation::final_answer when its confidence is high enough); 'needs_user_review' findings stay in the user queue. Hands each to kg_resolution_manager which reads the recommendation prose and applies the matching mutations from its full mutator suite (merge/delete/update/etc.). Closes the self-healing loop: pending â†’ investigated â†’ executed (mutations applied) | escalated (planner couldn't operationalize) | dismissed (take_action=False short-circuit, written by the investigator before this routine runs).

### `kg_finding_cluster_resolve`

**Cadence:** daily at 03:30

Reads pending findings, groups by primary_node_id, calls kg_finding_cluster_resolver to verify which candidate clusters share ONE root question. Confirmed leads get synthesized root_question; siblings get superseded_by stamped (hidden from default UI). Resolving a lead cascades to its siblings. Runs after wiki_nightly_refresh (03:00) so wiki_contradiction findings of the same night are eligible for clustering.

### `kg_state_date_drain`

**Cadence:** daily at 03:15

Drains pending state_missing_dates findings through kg_investigation_manager with the specialized state-dates brief (dated neighbors, conversation-evidence window, sibling states). Investigator emits proposed_action.op='fill_dates' with confidence; high-confidence ones get auto-applied by kg_finding_executor_drain. Limit 8/day keeps LLM cost bounded — raise if the backlog (hundreds today) doesn't shrink fast enough.

### `kg_wiki_inference`

**Cadence:** daily at 04:15

Reads the 10 freshest wiki pages and runs wiki_connection_investigator to propose edges (and sometimes new target nodes) the page implies but the KG doesn't yet have. Proposals flow through the standard claim_proposal pipeline so the promoter's merge gates apply. Runs after wiki_nightly_refresh (03:00) and the cluster/executor drains so it sees the freshest pages and only generates novel proposals.

### `kg_state_decay`

**Cadence:** daily at 02:45

Auto-closes State/Event nodes whose TTL has elapsed (uses attributes.last_observed + attributes.ttl, GRACE_DAYS=3). Runs after proposal_promoter (02:30) so freshly-promoted nodes are in the candidate set, and before wiki_nightly_refresh (03:00) so closures flow into the wiki the same night. Nightly cadence so the graph doesn't accumulate ghost-open states between weekly maintenance runs.

### `kg_goal_outcome_detect`

**Cadence:** daily at 02:55

Scans recent chat for explicit 'I did X' / 'I gave up X' statements about active Goal nodes. Closes matching Goals as terminal (achieved/abandoned) with end_date set. Conservative — only fires on direct user statements at confidence >= 0.7. Silent abandonment is goal_dormancy_sweep's job. Limit 10 Goals/day to keep LLM cost bounded.

### `kg_goal_dormancy_sweep`

**Cadence:** daily at 02:50

Flips long-silent Goal nodes from active â†’ dormant. Reversible — any re-observation in the promoter flips back to active. NEVER sets end_date (that's terminal achievement/abandonment, handled separately). Threshold = max(ttl.estimated_duration_days * 1.5, 30) days, capped at 730d. Default 90d when no TTL. Lifelong-value Goals (label/category contains 'lifelong', 'value', 'be a good', 'stay healthy', etc.) are skipped — only explicit closure can end those. Runs alongside state_decay (02:45) so card/wiki refreshes downstream see consistent goal_status.

### `kg_importance_rater`

**Cadence:** every 1800s

Rates any node/edge with NULL importance via me::edge_importance_rater + me::importance_rater. Cheap, idempotent, runs every 30 min so the wiki refresh's importance pre-filter has scores by the time it runs.

## Entity cards & wiki

Generates and refreshes the human-readable views on top of the KG.

### `entity_cards_pipeline`

**Cadence:** daily at 00:20

Nightly maintenance: generate missing entity cards (no overwrites of core cards) and produce a conservative pruning dry-run report.

### `entity_card_maintenance_pipeline`

**Cadence:** daily at 02:00

Daily entity card quality scan: broken KG links, junk names, blank content, low confidence, stale content, no KG link. All findings written to entity_card_maintenance_finding. Flipped from weekly to daily 2026-05-07 so KG mutations get reflected in cards within ~24h instead of up to 7 days. (Until generation-on-mutation lands — see TODO 'cards should auto-regenerate like wiki does'.)

### `wiki_nightly_refresh`

**Cadence:** daily at 03:00

Scans the vault, regenerates only sections affected by KG node/edge updates since each page's generated_at, then runs the consistency critic. Runs after proposal_promoter (02:30) so newly-promoted nodes flow into the wiki the same night.

### `wiki_growth`

**Cadence:** daily at 03:30

Builds up to max_new_pages new prose pages each night for the highest-degree Entity nodes that don't yet have one. Runs after wiki_nightly_refresh (03:00). Pairs with refresh: refresh maintains existing pages, growth adds new ones. Tune max_new_pages to control daily LLM spend; raise temporarily if a one-off catch-up is needed.

## Fetching external data

Pull-ins from your other accounts (email, calendar, tasks, weather, news, location). Each is feature-gated — disabled until you connect the relevant integration in Settings.

### `fetch_email`

**Cadence:** every 300s

Periodic email fetch across all active Gmail OAuth accounts. Replaces BackgroundTaskManager round-robin data_fetch.

### `fetch_calendar_events`

**Cadence:** every 1620s

Periodic calendar sync. Defaults to local midnight â†’ +7 days when no dates provided.

### `fetch_todo_tasks`

**Cadence:** every 1860s

Periodic Google Tasks sync.

### `fetch_weather`

**Cadence:** every 660s

Periodic weather update. City defaults from resource_current_location.json.

### `fetch_news`

**Cadence:** every 1380s

Periodic news fetch from configured RSS feeds. Empty feed_urls falls back to DEFAULT_FEEDS.

### `fetch_scheduler_events`

**Cadence:** every 180s

Periodic internal scheduler event sync. Defaults to midnight â†’ +7 days.

### `location_refresh`

**Cadence:** every 900s

Rebuilds user location timeline from calendar events and updates resource_current_location.json. Used by weather tool and location_summary context.

### `situation_audit`

**Cadence:** every 1800s

Periodic background audit of all active context. Checks for contradictions, conflicts, and anomalies across data sources. Notifies user of findings via chat. Passive observer only — does not modify plans or tasks.

## Dayflow & insights

The daily orchestration layer + summary rollups.

### `dayflow_pipeline`

**Cadence:** every 60s

Step-based DayFlow pipeline.

### `dayflow_orchestrator_room_tick` *(disabled by default)*

**Cadence:** every 300s

DISABLED: Replaced by event-driven DayflowScheduler. Kept for reference.

### `daily_insights_pipeline`

**Cadence:** daily at 00:05

Nightly pipeline: archive context + tickets, build timeline, extract insights, apply to resource files, build assessment + summary. Runs steps sequentially in-process.

### `weekly_insights_pipeline`

**Cadence:** weekly Monday at 00:10

Reads the 7 most recent daily assessment summaries and synthesizes cross-day patterns and belief candidates.

## Belief engine

Tracks beliefs (domain-scoped opinions/preferences with confidence + decay). Disabled by default; flip on in `configs/belief_domains.yaml` per-domain.

### `belief_engine`

**Cadence:** daily at 00:30

Loops over every domain marked enabled in configs/belief_domains.yaml and updates that domain's beliefs from the last 14 days of daily insights + ticket signals. Adding / disabling a domain is a YAML edit, not a code change. Domain failures are logged but don't abort the rest. After all domains succeed, the adapter exports active beliefs to resource_user_beliefs.json inline — atomic with the run, no fixed-time race against a slow upstream.

### `belief_engine_export` *(disabled by default)*

**Cadence:** daily at 01:00

DISABLED for scheduled execution — belief_engine now exports inline at the end of its run. Pipeline registration kept so an operator can /run-routine belief_engine_export for a manual re-export. Content-guarded — file only rewrites if beliefs changed.

## Disabled examples / templates

Disabled out of the box. Copy these to `configs/routines/private/<id>.json`, customize, and flip enabled to use.

### `morning_briefing` *(disabled by default)*

**Cadence:** daily at 05:30

Compiled task: playwright headlines, emails, todos, daily summary agent, save to storage.

### `sample_morning_routine` *(disabled by default)*

**Cadence:** daily at 07:00

Example routine. Enable and adjust time to use.

### `timesheet_routine` *(disabled by default)*

**Cadence:** daily at 09:00

Manual/on-demand timesheet narrative routine. Scheduled execution remains disabled by default.

### `example_camera_motion_poll` *(disabled by default)*

**Cadence:** every 60s

Polls Ring's recent-events history for one camera. On any new motion/ding event since the watermark, captures a fresh snapshot via the Ring bridge → publishes ring_snapshot_captured → camera_dispatch routes to the analyzer + skill configured for this camera in configs/cameras.json.

