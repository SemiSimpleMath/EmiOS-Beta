# KG Health Components — full inventory

Canonical reference of every component involved in maintaining the
knowledge graph + its derivatives (wiki pages, entity cards). When a
new finding shows up, this is the doc you want open.

## Honest assessment of the current state

This system has accumulated organically. Specific overlaps to be
aware of before reading the tables below:

- **Two paths apply mutations.** `kg_investigation_manager` +
  `kg_mutation_manager` is the autonomous cron pair (split read/write).
  `kg_resolution_manager` is the user-prose-driven path that collapses
  investigate + mutate + regen into one manager. Plan: drop
  `kg_resolution_manager` once the cron pair carries the
  conflict-triage doctrine. **Treat `kg_resolution_manager` as
  transitional.**
- **State decay is owned by the daily `kg_state_decay` routine
  (02:45 every night).** The weekly maintenance pipeline used to also
  call `step_state_decay` on Mondays, which double-fired the same
  code an hour after the daily routine had just run. Dropped from the
  weekly pipeline as of pipeline.py:91-94.
- **Cards have their own finding lifecycle.** Card findings live in
  `entity_card_maintenance_finding` (separate table), reviewed at
  `/entity-card-maintenance` (separate route). No
  investigator+executor loop on the card side — just SQL scans + manual
  review. Asymmetric to the KG side.
- **Two card pipelines are a producer/auditor pair**, not duplicates:
  `entity_cards` (daily 00:20) generates cards via LLM;
  `entity_card_maintenance_pipeline` (weekly Tue 02:00) audits them
  via SQL. Confusingly similar names — see "Two card pipelines" note
  below.

## Daily timeline (cron, local times)

```
DAILY (every night):
00:20  entity_cards (pipeline)            ← GENERATE cards (PageRank → write → prune)
02:00  kg_pipeline (pipeline)             ← INGEST: chat → claim_proposals
02:30  proposal_promoter (function)       ← MERGE BOT: promote claim_proposals to live KG nodes/edges
02:45  kg_state_decay (function)          ← daily lightweight close stale States (also a step in the weekly pipeline)
02:50  kg_goal_dormancy_sweep (function)  ← active → dormant
02:55  kg_goal_outcome_detect (function)  ← "I finished X" → terminal close
03:00  kg_finding_backlog_drain (function) ← investigator runs on findings (5/day FIFO)
03:00  wiki_nightly_refresh (function)    ← regen pages whose bullets changed
03:15  kg_state_date_drain (function)     ← investigator + specialized state-date brief (8/day)
03:30  kg_finding_cluster_resolve (function) ← group N redundant findings into 1 lead
03:30  wiki_growth (function)             ← write new pages for high-degree entities
03:45  kg_finding_executor_drain (function) ← executor applies investigated findings (10/day)
04:15  kg_wiki_inference (function)       ← propose new edges from wiki page prose

WEEKLY:
Mon 02:00  kg_maintenance_pipeline (pipeline)             ← heavy audit: orphan/embedding/dup/pagerank/desc/state-decay/missing-dates
Tue 02:00  entity_card_maintenance_pipeline (pipeline)    ← AUDIT cards (6 SQL scans)

EVERY 30 MIN:
       kg_importance_rater (function)     ← node + edge importance scores (60n / 50e per tick)
```

## The data row that links the cron pair

`kg_maintenance_finding` is the work queue.

```
pending  ←  scans + critics + investigators write findings here
   │
   │  kg_finding_backlog_drain (or kg_state_date_drain)
   ▼  → kg_investigation_manager fills investigation_report_json
investigated
   │
   │  kg_finding_executor_drain
   ▼  → kg_mutation_manager applies proposed_action
executed | escalated | dismissed
```

`superseded_by` is **orthogonal** to this state machine: when
`kg_finding_cluster_resolver` distills a cluster, siblings get
`superseded_by = lead.id` set and `get_findings()` hides them by
default. The lead still cycles through pending → investigated → final.

## Component tables

### Ingestion (chat / events → KG)

| Component | Type | Purpose | Reads | Writes |
|---|---|---|---|---|
| `kg_pipeline` | pipeline (cron 02:00) | Master ingestion: chat → resolved → windows → extractions → enrichments → claim_proposals | `unified_log_2026` | resolved_message, kg_window, kg_window_extraction, kg_window_enrichment, claim_proposal* |
| `kg_pipeline/steps/resolve_messages` | step | Resolve entity references in raw messages | unified_log | `kg_resolved_message` |
| `kg_pipeline/steps/segment_messages` | step | Group resolved messages into coherent windows | resolved messages | `kg_window`, `kg_window_message` |
| `kg_pipeline/steps/critique_and_extract` | step | LLM extracts typed claims (uses `fact_extractor` agent) | windows | `kg_window_extraction` |
| `kg_pipeline/steps/enrich_extraction` | step | Canonicalize + date-resolve extracted claims | extractions | `kg_window_enrichment` |
| `kg_pipeline/steps/write_proposals` | step | Convert enrichments to `claim_proposal*` rows | enrichments | claim_proposal tables |
| `proposal_promoter` (`app/assistant/kg/proposal_promoter.py`) | function (cron 02:30) | **The merge bot.** Promote eligible `claim_proposal` rows to live `kg_node` + `kg_edge`. Hosts the same-node detector and recurring-event gate. | claim_proposals | kg_node_metadata, kg_edge_metadata, kg_node_evidence |

### Maintenance pipeline (the big container, cron 02:00)

`kg_maintenance_pipeline` orchestrates these steps in order:

| Step | Purpose | Output |
|---|---|---|
| `step_orphan_scan` | Find nodes with zero edges | `orphan_node` findings |
| `step_context_embedding_backfill` | Backfill missing ChromaDB embeddings | (silent fix) |
| `step_duplicate_scan` | Mechanical pair-detection by label/edge similarity → LLM verification | `duplicate_node` findings |
| `step_pagerank` | Recompute graph PageRank scores | `kg_node_metadata.pagerank_score` |
| `step_description_fill` | Generate `node.description` via `description_creator` | Updates `description` via `persist_description` |
| `step_missing_dates_scan` | Find State/Event nodes missing one or both dates | `state_missing_dates` findings |

(`step_state_decay` used to be a step here too. Removed — it's owned by the daily `kg_state_decay` routine to avoid the Monday double-fire.)

### Standalone cron functions

| Routine | Function | Purpose | Manager invoked |
|---|---|---|---|
| `kg_state_decay` | `step_state_decay.run` | Auto-close State/Event nodes whose TTL elapsed; emits `state_auto_closed` findings (only noteworthy) | (none — pure SQL) |
| `kg_goal_dormancy_sweep` | `step_goal_dormancy.run` | Flip silent Goals from `active` to `dormant` | (none) |
| `kg_goal_outcome_detect` | `step_goal_outcome_detect.run` | Detect "I finished X" / "I gave up" → terminal Goal closures | `goal_outcome_detector` agent (one-shot) |
| `kg_finding_backlog_drain` | `finding_processor.run_pending_findings` | Loop pending findings through the investigator (5/day FIFO) | **`kg_investigation_manager`** |
| `kg_state_date_drain` | `finding_processor.run_state_date_findings` | Same `kg_investigation_manager`, specialized brief for `state_missing_dates` findings (8/day) | `kg_investigation_manager` (specialized brief) |
| `kg_finding_cluster_resolve` | `step_cluster_resolve.run` | Group N redundant findings on the same primary_node into 1 lead | `kg_finding_cluster_resolver` agent (one-shot) |
| `kg_finding_executor_drain` | `finding_executor.run_investigated_findings` | Apply `proposed_action` from investigated findings (10/day) | **`kg_mutation_manager`** |
| `kg_wiki_inference` | `step_wiki_inference.run` | Read 10 fresh wiki pages, propose new KG edges | `wiki_connection_investigator` agent (one-shot) → claim_proposals |
| `kg_importance_rater` | rater function | Score node + edge importance for lens / wiki gates | (none) |

### Wiki maintenance

| Component | Type | Purpose | Reads | Writes |
|---|---|---|---|---|
| `wiki_nightly_refresh` (cron 03:00) | function | Regenerate pages whose bullet text would render differently from sidecar | KG; `bullet_index` sidecar | `wiki/<entity>.md`, sidecar |
| `wiki_growth` (cron 03:30) | function | Mint new pages for high-degree entities (importance + degree gates) | KG | new wiki pages |
| `page_writer` agent | LLM | Render a page section-by-section from KG neighborhood | KG | invoked by nightly_refresh / growth |
| `wiki_lead_writer` agent | LLM | Generate the lead paragraph | KG | invoked by page_writer |
| `wiki_section_tagger` agent | LLM | Classify a section's topic before rendering | section text | invoked by page_writer |
| `wiki_consistency_critic` agent | LLM | Read a fresh page + KG neighborhood, flag contradictions | wiki + KG | `wiki_contradiction` findings |
| `wiki_inclusion_critic` agent | LLM | Decide if an entity warrants a page (importance gate) | KG | invoked by growth |
| `wiki_page_reviewer` agent | LLM | Quality review on a fresh page (separate from contradictions) | wiki | invoked by nightly_refresh |
| `wiki_connection_investigator` agent | LLM | Read a wiki page, propose new KG edges from prose | wiki + KG | claim_proposals (via `step_wiki_inference`) |
| `wiki_renderer` module | code | Render markdown from KG neighborhoods (no LLM) | KG | wiki vault |

### Two card pipelines (producer + auditor)

Despite confusing names, these are **distinct**:

| Component | Type | Purpose | LLM? | Findings table |
|---|---|---|---|---|
| **`entity_cards`** (cron 00:20) | pipeline (3 steps: PageRank → Generate → PruneDryRun) | **Generates** cards | yes | n/a |
| **`entity_card_maintenance_pipeline`** (weekly Tue 02:00) | pipeline (6 SQL-only scan steps) | **Audits** existing cards | no | `entity_card_maintenance_finding` |

Maintenance steps + their findings:

| Step | Output |
|---|---|
| `step_blank_content_scan` | `blank_card` findings (medium priority) |
| `step_broken_link_scan` | `broken_card_link` findings (high priority → deactivate) |
| `step_junk_name_scan` | `junk_card_name` findings (high priority → deactivate) |
| `step_low_confidence_scan` | `low_confidence_card` findings (medium priority) |
| `step_no_link_scan` | `unlinked_card` findings (low priority) |
| `step_stale_content_scan` | `stale_content` findings (low priority) |

`entity_card_critic` agent runs as part of card *generation* (inside the producer pipeline's critic gate), not maintenance.

### Manager surfaces

| Manager | Role | Tools | Caller |
|---|---|---|---|
| **`kg_investigation_manager`** | read-only investigator | `kg_query` (recently curated from 4) | `kg_finding_backlog_drain`, `kg_state_date_drain`, `/kg-maintenance/api/finding/<id>/investigate` |
| **`kg_mutation_manager`** | mutator | `kg_merge_nodes`, `kg_rename_label`, `kg_update_node_field`, `kg_finding_resolve`, `kg_finding_escalate` | `kg_finding_executor_drain`, `/kg-maintenance/api/finding/<id>/execute` |
| `kg_resolution_manager` *(transitional — plan to drop)* | read + mutate + regen | `kg_query`, `kg_close_state`, `kg_update_node_field`, `regenerate_entity_card`, `refresh_wiki_page` | `/kg-maintenance/api/finding/<id>/resolve_with_prose` |
| `kg_dev_manager` | free-form admin console | full mutator suite | `kg_dev_room` chat |
| `kg_dev_room_manager` | dispatch front for `kg_dev_manager` | `kg_dev_manager` (as a tool) | `kg_dev_room` chat |
| `kg_query_manager` | read-only sub-tool exposing `kg_query` | `kg_query` | other managers via delegation |
| `kg_explorer_manager` | multi-step KG exploration with provenance | exploration tools | `emi_team_manager` delegation |
| `kg_team_manager` | bundle of query/mutation/explorer for `emi_team_manager` to delegate to | child managers | `emi_team_manager` |

`fact_gate_manager` exists but is for **TaskIR task-spec gates**, NOT
KG health. Listed only here so the next reader doesn't assume it
belongs.

### Skills + resources used by these managers

| Artifact | Path | Used by |
|---|---|---|
| `kg-conflict-triage` skill | `skills/kg-conflict-triage/SKILL.md` | `kg_resolution::planner` (statically bound today). Auto-injects on contradiction-flavored task keywords for any agent with `accept_auto_skills: true`. **Plan:** also bind to `kg_investigation::planner` once consolidation lands. |
| `resource_kg_node_reading` | `resources/instructions/resource_kg_node_reading.md` | 10 agents: entity_card_summarizer, kg_dev/chat_gate, kg_investigation/planner, kg_maintenance/duplicate_detector, kg_mutation/planner, kg_resolution/planner, knowledge_graph_add/node_merger, wiki_consistency_critic, wiki_lead_writer, wiki_writer |
| `resource_kg_principles` | `resources/instructions/resource_kg_principles.md` | (orphaned after consolidation — kept on disk for reference) |
| `resource_wiki_principles` | `resources/instructions/resource_wiki_principles.md` | (orphaned) |
| `resource_entity_card_principles` | `resources/instructions/resource_entity_card_principles.md` | (orphaned) |

## Out of scope (mentioned for orientation)

- **`belief_engine`** — separate domain (beliefs vs KG nodes). Adjacent
  surface but its own pipelines + tables. Not a KG-health component;
  use `docs/architecture/16_BELIEF_ENGINE.md`.
- **DB tables vs feature flags vs pipeline IDs sharing the name
  "entity_cards":** the DB table `entity_cards`, the user-settings
  feature flag `"entity_cards"`, and the pipeline_id `"entity_cards"`
  are three different namespaces that happen to share a string.

## How to find the producer of a finding

| Finding type | Producer | Findings table |
|---|---|---|
| `state_auto_closed` | `step_state_decay` | `kg_maintenance_finding` |
| `state_missing_dates` | `step_missing_dates_scan` | `kg_maintenance_finding` |
| `duplicate_node` | `step_duplicate_scan` | `kg_maintenance_finding` |
| `orphan_node` | `step_orphan_scan` | `kg_maintenance_finding` |
| `wiki_contradiction` | `wiki_consistency_critic` (during `wiki_nightly_refresh`) | `kg_maintenance_finding` |
| `synthetic_fact_proposal` | `wiki_connection_investigator` (during `kg_wiki_inference`) | `kg_maintenance_finding` |
| Cluster lead with `cluster.root_question` | `kg_finding_cluster_resolver` | `kg_maintenance_finding` |
| `blank_card`, `broken_card_link`, `junk_card_name`, `low_confidence_card`, `unlinked_card`, `stale_content` (card variant) | `entity_card_maintenance_pipeline` steps | `entity_card_maintenance_finding` |

## How a finding closes

| Status | Reached by |
|---|---|
| `pending` (initial) | producer wrote it |
| `investigated` | `kg_investigation_manager` populated `investigation_report_json` |
| `executed` | `kg_mutation_manager` applied `proposed_action` (or `kg_resolution_manager` finished a prose-driven resolution) |
| `dismissed` | investigator/resolver decided false positive (with reason) |
| `escalated` | flagged for human review (banned mutation, conflicting prose, etc.) |

`superseded_by` is set independently when `kg_finding_cluster_resolver`
folds the row into a lead; the row's status still cycles normally
through the lead's resolution.

## Outstanding issues / known bugs

- Some `kg_node_metadata` rows have inverted date intervals
  (`start_date > end_date`) from a legacy migration. Audit:
  `SELECT id, label, start_date, end_date, created_at FROM
  kg_node_metadata WHERE start_date > end_date;` — fixed individually
  as discovered (e.g. `775aafdb`).
- `wiki_consistency_critic` regularly produces false positives that
  don't read `end_date` (the present-tense canonical trap). The
  `kg-conflict-triage` skill calls this out — investigator
  dismissals should educate the next reader.
- `kg_resolution_manager` and `kg_investigation_manager` duplicate
  investigation logic. Consolidation plan: harden the investigator,
  share the conflict-triage skill, drop `kg_resolution_manager`'s
  investigation responsibilities (or drop the whole manager).

## Plan: 1 good investigator + 1 good executor

The autonomous self-healing loop should be:

1. **`kg_investigation_manager`** — picks up findings, applies the
   `kg-conflict-triage` skill (data-quality sanity check, three-bucket
   triage, stop-early discipline, provenance walk via SQL), writes
   structured `investigation_report_json` with verdict + confidence
   + proposed_action.
2. **`kg_mutation_manager`** — picks up investigated findings,
   applies the proposed_action via curated mutator + regen tools.
   Closes / escalates / dismisses based on the verdict.

Both share the conflict-triage doctrine. Different tool kits
(read-only vs read+mutate), same brain. `kg_resolution_manager`
becomes redundant; the cluster prose UX gets reframed to mint a
synthetic finding the cron loop processes.
