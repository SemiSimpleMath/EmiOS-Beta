# Wiki Generator — Architecture

## What it is

The wiki generator projects the user's knowledge graph into a Markdown vault on disk (default `<your-home>/EmiWiki`). One file per Entity, Obsidian-compatible, with YAML frontmatter and prose body. Pages are produced by an LLM-driven pipeline that walks the KG neighborhood around an entity, renders bullets, classifies them into biographical sections, writes per-section prose, and stitches the result with a Wikipedia-style lead. A nightly routine refreshes only the sections affected by KG changes since each page's `generated_at`. A Flask blueprint at `/wiki/` serves the vault as a Wikipedia-style read surface.

## End-to-end flow

```
                        +---------------------------+
                        |   kg_node_metadata        |
                        |   kg_edge_metadata        |  (live KG)
                        +-------------+-------------+
                                      |
                                      v
                kg_projection.get_entity_neighborhood(label)
                                      |
                                      v
                       EntityNeighborhood dataclass
              (relationships, events, beliefs, goals,
               inbound_relationships, inbound_events,
               misc_*, kg_gaps, entity_card)
                                      |
            +-------------------------+--------------------------+
            |                         |                          |
            v                         v                          v
   wiki_renderer.render_page    kg_projection.            collect_entity_window_ids +
   (deterministic markdown,     render_bullets             build_window_excerpts
    WIKI:DET section markers)   (Bullet[]: text +          (raw user-message excerpts
            |                    provenance)               grounding the writer)
            v                         |
   <vault>/<Entity>.md                v
   (rough page)               wiki_section_tagger
                              (LLM classifies each Bullet
                               into 0..N taxonomy keys)
                                      |
                                      v
                              <vault>/tags/<Entity>.json
                              (sidecar; bullet_key -> [keys])
                                      |
                                      v
                              group_bullets_by_section
                                      |
                                      v
                       per-section wiki_writer calls
                       (one LLM call per section slice)
                                      |
                                      v
                              <vault>/section_outputs/<Entity>.json
                              (sidecar; section_key -> prose)
                                      |
                                      v
                              wiki_lead_writer
                              (reads stitched body, writes lead)
                                      |
                                      v
                              apply_references
                              ({node:<id>} -> [N] + ## References)
                                      |
                                      v
                              <vault>/prose/<Entity>.md
                              (final article served by /wiki/<Entity>)
                                      |
                                      v
                              run_consistency_critic
                              (post-render fact-check;
                               files into kg_maintenance_finding)
```

## The vault

A vault is a flat folder of Markdown files plus three sidecar subdirectories. Default location is hard-coded as `DEFAULT_VAULT = Path(r"<your-home>/EmiWiki")` in `nightly_refresh.py:45`.

```
<vault>/
  <Entity>.md                        rough deterministic page (renderer output)
  index.md                           auto-rebuilt catalog grouped by category
  log.md                             append-only regen log
  prose/
    <Entity>.md                      final LLM-written article (served by /wiki/)
  tags/
    <Entity>.json                    bullet_key -> [section_key, ...] cache
  section_outputs/
    <Entity>.json                    section_key -> prose markdown cache
```

The rough `.md` and the prose `prose/<Entity>.md` are both well-formed Markdown with YAML frontmatter. The frontmatter contract (rendered by `wiki_renderer._render_frontmatter`, `wiki_renderer.py:147`) is:

| key | source | purpose |
|-----|--------|---------|
| `name` | `Node.label` | display name |
| `kg_node_id` | `Node.id` | back-reference for nightly diff and node-merge fixup |
| `node_type` | `Node.node_type` | typically `Entity`; passed to critic for tense awareness |
| `generated_at` | UTC ISO-8601 | watermark for `find_changed_neighborhood_nodes` |
| `auto_generated` | always `true` | UI flag |
| `do_not_edit` | always `true` | reminder for the human |
| `category` | `Node.category` | groups the index |
| `aliases` | `Node.aliases` | optional |
| `relationship_count` | outbound + inbound | counts for index display |
| `event_count` | outbound + inbound | counts for index display |
| `kg_gap_count` | `len(neighborhood.kg_gaps)` | hint that the source is incomplete |

The per-entity sidecar at `<vault>/section_outputs/<Entity>.json` is the cache that makes incremental refresh possible: it stores the LLM-written prose for each section keyed by section slug. `regenerate_affected_sections` reuses everything in this map and only re-calls `wiki_writer` for sections whose bullets reference a changed KG node. Loaded by `_load_section_outputs` and written by `_save_section_outputs` (`page_writer.py:482`).

## Page generation pipeline

There are three entry points into generation, all in `app/assistant/wiki_generator/`.

### 1. `regenerate_entity_page` — rough, deterministic

Defined in `wiki_writer.py:34`. Single fast pass:
1. `get_entity_neighborhood(label)` loads the KG neighborhood (`kg_projection/neighborhood.py:136`).
2. `wiki_renderer.render_page(neighborhood)` produces a complete Markdown string.
3. Writes `<vault>/<Entity>.md`, appends one line to `log.md`.

No LLM. The rough page is what the prose generators read. It carries `<!-- WIKI:DET <slug> -->...<!-- /WIKI:DET -->` section markers and `{node:<uuid>}` provenance tags inside each bullet. The `key_facts`, `see_also`, and `kg_gaps` sections are always rendered verbatim; downstream prose passes can reuse them without an LLM call.

### 2. `generate_prose_page_tagged` — full LLM regeneration (current production path)

Defined in `page_writer.py:502`. This is the path the nightly routine and the `/regenerate` UI call when no incremental work is possible. Steps:

1. Load the rough page (frontmatter + title + parsed `WIKI:DET` sections, used for the `see_also` slice).
2. Load the section taxonomy via `kg_projection.load_taxonomy` (resource: `resource_wiki_sections`).
3. Re-fetch the neighborhood and call `kg_projection.render_bullets` to get structured `Bullet` objects (text + provenance + window ids).
4. Run **`wiki_section_tagger`** (gpt-5.4-mini, `agents/wiki_section_tagger/`) over the bullets in batches. Cached by content hash via `tag_cache.bullet_key`. Sidecar: `<vault>/tags/<Entity>.json`.
5. `group_bullets_by_section` inverts to `{section_key: [bullets]}`. A bullet may land in multiple sections.
6. For each section in the taxonomy that has bullets, build a mini rough (`## <Title>\n\n` + bullets) and call **`wiki_writer`** (gpt-5.4, `agents/wiki_writer/`) once. Returns `page_markdown` per section. The sentinel `(nothing new to add)` causes the section to be dropped.
7. Persist the per-section outputs to `<vault>/section_outputs/<Entity>.json`.
8. Build the article body by concatenating section prose in taxonomy order, then `see_also` from the rough page.
9. Call **`wiki_lead_writer`** (`lead_writer.generate_lead`) with the assembled body. Lead is best-effort; failures yield `""` and the page renders without one.
10. Stitch frontmatter + title + lead + body, then run `apply_references` to convert `{node:<uuid>}` markers into numbered `[N]` footnotes plus an appended `## References` section.
11. Write `<vault>/prose/<Entity>.md`.

(The older `generate_prose_page` and `generate_prose_page_sectioned` writers have been removed — `generate_prose_page_tagged` is the only prose entry point.)

### 3. `regenerate_affected_sections` — incremental

Defined in `page_writer.py`. Used by the nightly refresh whenever a baseline `section_outputs` sidecar exists. Steps:

1. Load the cached sidecar; fall back to full `generate_prose_page_tagged` if absent.
2. Re-fetch the neighborhood, re-render structured bullets, re-tag (cache hits avoid LLM calls for unchanged bullets).
3. **Dirty detection via bullet-text diff**: a `bullet_index` sidecar (`<vault>/bullet_index/<entity>.json`) records the bullet keys (`sha256(text)[:16]`) present at the LAST successful rewrite. Compare against currently-rendered keys to compute `added` and `removed` sets. A section is dirty iff it contains an added or removed bullet. Unchanged keys imply identical bullet text — even when the underlying node was touched for unrelated reasons (importance recalc, description refresh, new edges that don't touch this neighborhood). The `changed_node_ids` argument survives only as the cheap pre-filter at the caller level (`refresh_one_page`) that decides whether to even open the page's neighborhood.
4. **Critic gate**: addition-driven dirty sections go through `wiki_inclusion_critic` per added bullet. Removal-driven sections always rewrite (no bullet to evaluate, and the existing prose mentions a fact that's gone).
5. Re-call `wiki_writer` for approved-dirty sections only; leave the cached prose for everything else.
6. Persist new section_outputs, restitch in taxonomy order, re-run the lead writer (because the body it summarizes may have changed), write the new prose page, then save the updated `bullet_index` (after section_outputs so a crash mid-rewrite leaves the old index in place — next run re-detects and self-heals).

## Section taxonomy

Authoritative file: `resources/user/resource_wiki_sections.json`. Loaded via `kg_projection.load_taxonomy(scope_context=...)`, which prefers the `ResourceManager` and falls back to the JSON file. Each entry is a `SectionSpec(key, title, description)` (`kg_projection/sections.py:30`). `key` is the slug used as the dict key in tagger output and as the `section_slug` passed to the writer; `title` becomes the `## <Title>` heading; `description` is one line shown to the tagger to disambiguate.

The taxonomy is intentionally biographical (Wikipedia-style life-stage sections like `early_life`, `education`, `career`, `marriage_and_family`, `daily_life`, `health`, `travel_and_events`, `personal_projects`, `contact`). Tagging an entity's bullets against this fixed vocabulary is what gives every page a consistent shape regardless of which KG buckets the facts came from.

> Note: the `contact` key in the taxonomy is consumed by the entity card generator, not the wiki writer. The tagger may emit it, but the wiki section-stitch loop simply has no prose for it because contact details are explicitly omitted by the writer prompt (`agents/wiki_writer/prompts/system.j2`).

## Inbound vs outbound edges

Passive entities (locations, institutions, schools) rarely originate KG edges — they are the target of activity. `EntityNeighborhood` carries both directions:

- `relationships` / `events` — outbound (this entity is the source of the State/Event hub edge).
- `inbound_relationships` / `inbound_events` — inbound (a State or Event hub points at this entity, e.g. `Education-State --at_institution--> Westview Arts`).

`get_entity_neighborhood` (`kg_projection/neighborhood.py:181`) classifies inbound edges. Both `wiki_renderer._render_relationships_section` and `kg_projection.bullets._state_connection_bullet` rephrase inbound bullets as "site of <relationship>" / "site of <event>" so the writer correctly understands the page's subject is the target, not a participant. Without this, a school's wiki page would be empty even when many people attend it. The same enrichment (counterpart entities one hop further out) applies in both directions.

## Lead writer

`lead_writer.generate_lead` (`lead_writer.py:41`) calls the **`wiki_lead_writer`** agent (`agents/wiki_lead_writer/`, gpt-5.4) after the per-section prose is stitched. It receives `entity_name`, `entity_type`, and `article_body` (everything below the H1, no frontmatter or title). It returns 1-2 short paragraphs that get inserted between the H1 and the first H2.

The lead runs **after** the body for two reasons. (1) Wikipedia-style leads summarize the article, so the writer needs the article to read. (2) The body writer has already made tense decisions per-fact based on each State's validity window; the lead just copies that tense rather than re-deciding from KG data.

The lead pass is error-safe by design: any exception or empty output yields `""` and the page renders without a lead. The `incremental` regen path also re-runs the lead because refreshed sections may have changed material it summarizes (`page_writer.py:818`).

## Consistency critic

`consistency_critic.run_consistency_critic` (`consistency_critic.py:275`) calls the **`wiki_consistency_critic`** agent (`agents/wiki_consistency_critic/`, gpt-5.4-mini) on the rendered prose page. The critic reads only the prose — it cannot see the graph. That is intentional: the wiki is a QA surface, and internal contradictions there often reveal real KG issues (tense errors, duplicate nodes, classifier mistakes) that were invisible at the graph level.

It checks four issue types: `contradiction`, `tense_mismatch`, `duplicate_entity`, `impossible_sequence`, plus catch-all `other`. A `ground_truth_block` is assembled from the entity card (summary + key_facts) and, when the page subject is the primary user, from `resource_user_data` (home city / state / country, current job, birthdate, important_people). Prose claims that disagree with that block are flagged as `contradiction` with `source_kind` set to one of `entity_card_summary`, `entity_card_key_fact`, `user_profile`. Internal-only findings get `source_kind: "internal"`.

Findings are written to the `kg_maintenance_finding` table with `finding_type = "wiki_contradiction"` (`consistency_critic.py:33`). Priority is `high` for `contradiction` / `impossible_sequence`, otherwise `medium`. The `evidence_json` blob carries the quoted text plus the surrounding paragraph, section heading, and line number computed by `_locate_quote_context` so a reviewer (or downstream investigator) doesn't have to reload the wiki page. Duplicate findings (same `quoted_text` for the same `primary_node_id`, still `pending`) are skipped.

If `investigate_immediately=True` (the default) and any finding is saved, `kg_investigator.finding_processor.investigate_findings` is called inline so a structured investigation report is ready before a human looks at the queue. Capped by `max_investigations=5` per page to bound LLM cost.

## Nightly refresh

`nightly_refresh.run_nightly_wiki_refresh` (`nightly_refresh.py:224`) is wired as a routine function:

```json
"id": "wiki_nightly_refresh",
"runner": "function",
"spec": {
  "function_name": "wiki_nightly_refresh",
  "run_critic": true,
  "vault_path": "<your-home>/EmiWiki"
},
"run_policy": { "type": "daily", "time_local": "03:00", "quiet_hours_ok": true }
```

Scheduled at 03:00 local, after the `proposal_promoter` at 02:30, so newly-promoted nodes flow into the wiki the same night. See `06_PIPELINES_AND_ROUTINES.md` for routine semantics.

The flow per scan:

1. `_list_prose_pages` walks `<vault>/prose/*.md` and reads frontmatter (label, path, `generated_at`, `kg_node_id`).
2. **`_apply_pre_refresh_revision_log_effects`** (`nightly_refresh.py:91`) consults `kg_revision_log` for two op types between the earliest page's `generated_at` and now:
   - `merge_nodes`: a page whose `kg_node_id` was the `fold_id` is silently orphaned (the row is gone, so node-lookup returns `None` and the timestamp diff would be empty). Chained merges (A→B then B→C) are collapsed before the rewrite, so an A-page lands on C's id directly. Frontmatter is rewritten in place.
   - `delete_edge`: a deleted edge has no `updated_at`, so the timestamp diff cannot see it. Endpoints are pre-collected from `before_json`; the non-self endpoint is added to the `force_changed_node_ids` set for the page entity that still exists.
3. For each page, `refresh_one_page` (`nightly_refresh.py:170`):
   - If `generated_at` is missing, do a full regen (`regenerate_entity_page` + `generate_prose_page_tagged`).
   - Otherwise call `find_changed_neighborhood_nodes(entity_node_id, since_ts)` (`kg_projection/change_detection.py:25`). It returns ids whose `updated_at > since_ts`, walking the entity itself, every directly-connected node, both edges, and counterpart entities one hop past State/Event hubs (a counterpart's update can change the bullet that renders the hub).
   - Merge in any `force_changed_node_ids` from the revision-log step.
   - If empty, skip. Otherwise `regenerate_entity_page` (refresh rough) + `regenerate_affected_sections` + (optional) `run_consistency_critic`.

Output is a per-page summary plus aggregate counts (`pages_scanned`, `updated`, `unchanged`, `full_regen`, `errors`).

## Canonical-sentence contract

Every State / Event / Goal node has its `original_sentence` rewritten to **present-tense canonical** form by the `fact_canonicalizer` agent at promotion time (see `09_KG_PIPELINE.md`, Stage 5). The truth window — when the fact was actually true — lives in `start_date`, `start_date_prose`, `end_date`, `end_date_prose`, and `valid_during`, NOT in the verb tense of the sentence.

The wiki writer prompt (`agents/wiki_writer/prompts/system.j2`) enforces the readers' side of this contract explicitly:

- A State with `valid_during: ongoing` and no `end_date` is currently true → present tense.
- A State with `end_date` / `end_date_prose`, or `valid_during: one-off`, has ended → past tense.
- An Event is always past.
- A State with **no validity window at all** (no `since` clause, no `valid_during`, no `end_date`) has unknown currency → the writer must hedge ("after moving to the new city, Jamie enrolled at..."), or omit, or anchor on `start_date_prose` with past framing. Confident present tense is forbidden without explicit `valid_during: ongoing` or a recent `since <date>`.
- The blockquote (`> ...`) preserves the speaker's wording for voice/quoting only — it is not a fresh claim about the world.
- Source-relative phrases ("last year", "this Wednesday") were already resolved INTO `start_date` against the utterance time. Re-applying them on top of the resolved date is a double-count and produces the wrong year.

The lead writer prompt (`agents/wiki_lead_writer/prompts/system.j2`) restates the same contract: it must copy the body's tense rather than re-deriving from KG data. A present-tense quote inside the body is NOT evidence of currency — only the body's own tense decision (which the body writer made after consulting the validity window) is.

The metadata bracket appended to each bullet by `kg_projection.bullets._read_metadata_bits` (`bullets.py:380`) surfaces the inputs the writer needs to make this call: `type`, `valid_during`, `start_date_prose`, `end_date`, `end_date_prose`, `utterance`, `date_confidence`. The `wiki_renderer._read_metadata_bits` does the same for the rough page (`wiki_renderer.py:102`).

> Note: the canonical-sentence model is shipping for newly-promoted nodes, but historical nodes promoted before the canonicalizer was wired up may still carry source-POV sentences. The hedging rules in the writer prompt are the safety net.

## /wiki/ Flask UI

Blueprint registered in `app/routes/wiki_viewer.py`. Routes:

| Route | Handler | Purpose |
|-------|---------|---------|
| `GET /wiki/` | `wiki_index` | List all articles, grouped by frontmatter `category` |
| `GET /wiki/random` | `wiki_random` | 302 to a random article |
| `GET /wiki/search?q=<q>` | `wiki_search` | Substring scan over article bodies, returns snippets |
| `GET /wiki/<entity>` | `wiki_article` | Render `<vault>/prose/<entity>.md` (case-insensitive match) |
| `POST /wiki/<entity>/regenerate` | `wiki_article_regenerate` | Force `regenerate_entity_page` → `generate_prose_page_tagged` → `run_consistency_critic` for one page |

The viewer reads from `WIKI_DIR = Path(r"<your-home>/EmiWiki/prose")` (`wiki_viewer.py:31`). Markdown is rendered through Python `markdown` (extensions: `extra`, `toc`, `sane_lists`, `tables`). After rendering:
- `_wikilink_sub` rewrites `[[Name]]` and `[[Name|display]]` into `<a href="/wiki/Name" class="wikilink">`.
- `_node_marker_sub` turns any leftover `{node:<uuid>}` markers into small badges linking to `/kg/node/<uuid>`.
- `_extract_ref_map` parses the appended `## References` section into a `{N: node_id}` map; `_inline_ref_sub` then wraps every prose `[N]` token in an anchor to the corresponding KG node, skipping tokens already inside an `<a>` (so the References list itself is not double-wrapped).

Articles that don't exist in the vault render as a stub page rather than 404, so wiki-link targets to ungenerated entities still navigate cleanly.

## Source reconstruction (debug)

To trace a bad wiki sentence back to its origin, walk the canonical provenance chain: `kg_node_evidence.node_id` → `(window_id, source_id)` → `kg_window_message` → `unified_log_2026`. The kg_node_viewer's `_load_provenance` (`app/routes/kg_node_viewer.py:109`) is the reference implementation — it returns each contributing window with its full message list and the derived sentences the extractor produced. The legacy `source_reconstruct.py` helper and the `kg_chat_conversation_window` / `kg_chat_extracted_edge` tables it walked were retired in the 2026-04-26 KG-pipeline rebuild.

## Key files

| File | Role |
|------|------|
| `app/assistant/wiki_generator/__init__.py` | Re-exports neighborhood primitives from `kg_projection` for backward compatibility |
| `app/assistant/wiki_generator/page_writer.py` | Orchestrator: `generate_prose_page_tagged`, `regenerate_affected_sections`, `parse_rough_sections`, `strip_debug_scaffolding` |
| `app/assistant/wiki_generator/wiki_writer.py` | `regenerate_entity_page` (rough page), `rebuild_index`, `_append_log` |
| `app/assistant/wiki_generator/wiki_renderer.py` | Deterministic markdown renderer for the rough page (frontmatter + per-section blocks with `WIKI:DET` markers) |
| `app/assistant/wiki_generator/lead_writer.py` | `generate_lead` — calls `wiki_lead_writer` |
| `app/assistant/wiki_generator/nightly_refresh.py` | `run_nightly_wiki_refresh`, `_apply_pre_refresh_revision_log_effects`, `refresh_one_page` |
| `app/assistant/wiki_generator/consistency_critic.py` | `run_consistency_critic` — files `wiki_contradiction` findings, optional inline investigation |
| `app/assistant/wiki_generator/source_reconstruct.py` | QA helper to walk a wiki sentence back to its source chat window |
| `app/assistant/wiki_generator/references.py` | `apply_references` — `{node:<id>}` -> `[N]` + `## References` post-processing |
| `app/assistant/kg_projection/neighborhood.py` | `get_entity_neighborhood`, `EntityNeighborhood` and connection dataclasses |
| `app/assistant/kg_projection/bullets.py` | `Bullet` dataclass, `render_bullets` (single source of truth for bullet text + provenance) |
| `app/assistant/kg_projection/sections.py` | `SectionSpec`, `load_taxonomy`, `sections_as_prompt_list` |
| `app/assistant/kg_projection/tagger.py` | `tag_bullets` (LLM batched section classifier with cache + optional window grounding), `group_bullets_by_section` |
| `app/assistant/kg_projection/tag_cache.py` | `bullet_key` content hash, sidecar `load_tags` / `save_tags` |
| `app/assistant/kg_projection/change_detection.py` | `find_changed_neighborhood_nodes` — neighborhood-aware `updated_at` diff |
| `app/assistant/agents/wiki_writer/` | gpt-5.4 section prose writer; enforces the canonical-sentence reading contract |
| `app/assistant/agents/wiki_lead_writer/` | gpt-5.4 lead writer; reads the assembled body, emits 1-2 paragraphs |
| `app/assistant/agents/wiki_section_tagger/` | gpt-5.4-mini batched bullet → section classifier |
| `app/assistant/agents/wiki_consistency_critic/` | gpt-5.4-mini post-render fact checker |
| `app/routes/wiki_viewer.py` | Flask blueprint serving `/wiki/`, `/wiki/<entity>`, `/wiki/<entity>/regenerate` |
| `resources/user/resource_wiki_sections.json` | Section taxonomy (key / title / description) |
| `configs/routines.json` (`wiki_nightly_refresh`) | Routine entry that wires `run_nightly_wiki_refresh` into the daily 03:00 schedule |

## How to add a new section type

1. Append an entry to `resources/user/resource_wiki_sections.json`:
   ```json
   { "key": "<slug>", "title": "<Heading>", "description": "<one line for the tagger>" }
   ```
   `key` is the section slug; `title` becomes the `## Heading`; `description` is what the tagger reads to decide whether a bullet belongs.
2. The section is now live for new pages. The tagger picks it up the next time it runs (cached results for unchanged bullets are reused; only new bullets get tagged afresh, so adding a section does not invalidate the cache).
3. To force re-tagging for an existing page (e.g. you reworded a description and want old bullets reclassified): delete `<vault>/tags/<Entity>.json`. Next regen pays the full tagging cost for that entity.
4. To force re-prose for a section without re-running everything: delete that key from `<vault>/section_outputs/<Entity>.json`, then call the page's `/wiki/<Entity>/regenerate` endpoint.

## How to tweak the writer

The writer's behavior lives in `app/assistant/agents/wiki_writer/prompts/system.j2`. The prompt is the live spec for: voice (flat, factual), include/omit lists, tense rules from the canonical-sentence contract, the date-handling rules ("no date when no date is given"), the `{node:<uuid>}` paragraph-end provenance convention, and length budget (15-120 lines). Changes here are picked up on the next agent invocation; no code change needed.

If the writer ever regresses on a specific page, the diagnostic loop is:
- Inspect the rough `<vault>/<Entity>.md` — does the bullet metadata bracket carry the validity window the writer needs to choose tense correctly?
- Check `<vault>/section_outputs/<Entity>.json` — is the cached prose for one section the source of the issue?
- For a single quoted sentence, `source_reconstruct.reconstruct_source_window(sentence=...)` returns the original chat window and the sibling edges from the same extraction.

For deeper KG-level issues (a wrong claim that survives many regens, a duplicate node), the consistency critic's `wiki_contradiction` findings are the queue. See `22_KG_HEALTH_COMPONENTS.md` for the downstream investigator and executor flow, and `13_KG_MUTATOR_TOOLS.md` for the typed mutator tools.
