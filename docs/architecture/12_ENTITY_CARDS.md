# Entity Cards — Architecture (as-built, v2)

> v2 shipped 2026-05-10; the v1 `EntityCard` ORM and flat `entity_cards` table
> were retired the same day. This is the single as-built reference. The original
> v2 design memo lives at `12_ENTITY_CARDS_V2_DESIGN.md` (bannered, historical).

## What it is

An entity card is a structured **NOW-snapshot** of a person, place, or thing
the user knows — "what's true about this entity right now," rendered from the
entity's KG neighborhood into ordered sections of present-tense bullets. Cards
are the **structured companion to the narrative wiki** (`11_WIKI_GENERATOR.md`):
the wiki tells the historic story in prose; the card carries the current state
an agent should act on. Both project from the same KG; they differ on temporal
stance, not source.

A card is injected into an agent's prompt whenever its `card_title` or an alias
appears in the text the agent can see (`entity_card_injector.py`, via the v2
renderer through the `entity_cards.py` shim). One card per KG entity; the user's
own entity is deliberately card-less (see *user-self double-gating*).

## Three-layer mental model

| Layer | Temporal stance | Holds | Refresh |
|---|---|---|---|
| KG | all-time | every observation, open + closed states, all events | every ingest |
| **Entity card** | **NOW snapshot** | active states, definitional bio, current connections, contact | when an active fact changes (nightly) |
| Wiki | historic narrative | past events, closed-era states, biographical timeline | when a state closes / event added |

The card is a **NOW filter** over the KG; the wiki is the chronological
projection. Closed states and non-definitional past events are relegated to the
wiki, not the card.

## The NOW filter

`entity_card_v2.is_now_admissible(node_type, end_date, category, goal_status, locked_by_user_at, valid_currently=None)`
decides whether a connected KG node belongs on a card vs. the wiki. Rules (in
order):

- `locked_by_user_at` set → always admit (user lock overrides everything).
- `valid_currently is False` → reject (explicit closing evidence in the source
  window — set by `meta_data_add` at enrichment time — trumps structural inference).
- **State**: admit iff `end_date is None` or `end_date > utc_now()` (active).
- **Goal**: admit iff `goal_status in (None, 'active', 'in_progress', 'pending')`.
- **Event**: admit iff `category` ∈ `DEFINITIONAL_EVENT_CATEGORIES` (`birth`,
  `death`, `wedding`, `marriage`, `marriage_start`, `graduation`, `move`, `hire`,
  `meeting`, `creation`, `relationship_beginning`, `family_formation`) — events
  that anchor the present even though they happened in the past.
- **Property**: always admit (timeless attribute).
- **Entity / Concept / Pod**: reject — they're link targets, not bullets.

The filter is applied to the *other* side of each edge during fact collection,
so a state that closed drops its bullets at the card's next rebuild (there is no
per-bullet incremental refresh; it was removed 2026-10-05).

## Storage schema

`app/assistant/entity_management/entity_card_v2.py`. Four tables — section/bullet
structured, not a flat summary row:

| Table | Role |
|---|---|
| `entity_card_v2` | one row per card |
| `entity_card_section` | one row per section per card |
| `entity_card_bullet` | one row per bullet per section |
| `entity_card_bullet_source_node` | `(bullet_id, node_id)` reverse-lookup |

**`entity_card_v2`** (`EntityCardV2`): `id`, `entity_node_id` (FK →
`kg_node_metadata.id`, `ondelete='CASCADE'`, **nullable + unique** — NULL = a
user-authored non-kg card, and SQLite UNIQUE permits many NULLs), `entity_type`,
`is_active`, denormalized `card_title` + `card_aliases` (JSON), and
`last_built_at` / `last_full_rebuild_at` / `last_incremental_at`. Retrieval scans
`card_title` directly so kg and non-kg cards share one path. Two indexes matter:
`ix_entity_card_v2_active`, and **`uq_entity_card_v2_active_title`** — a partial
unique index (`sqlite_where='is_active = 1'`) enforcing *at most one active card
per title*; the render path looks cards up by title, and two active rows would
crash chat_gate's `one_or_none()`.

**`entity_card_section`** (`EntityCardSection`): `card_id` (FK, CASCADE),
`section_name`, `position`, optional `intro_text` + `intro_source_hash` (hash of
the section's bullet sequence, drives intro-regen). Unique on `(card_id, section_name)`.

**`entity_card_bullet`** (`EntityCardBullet`): `section_id` (FK, CASCADE),
`position`, `bullet_text`, `source_node_ids` + `source_edge_ids` (JSON),
`source_hash` (drives drift detection), `confidence_tier` (inherits the **weakest**
source tier: `axiom > confirmed > provisional > inferred`).

**`entity_card_bullet_source_node`** (`EntityCardBulletSourceNode`): denormalized
`(bullet_id, node_id)` PK with an index on `node_id`, so "which bullets reference
this changed node" is an O(log n) join instead of a JSON scan of every bullet.
Every build writes it; since the incremental refresh that read it was removed
(2026-10-05), no code reads it (nor writes `last_incremental_at`).

### Section templates + bullet caps

`SECTION_TEMPLATES` maps an entity type to an ordered `[(section_name, kind)]`
list; `section_template_for(entity_type, category)` picks one (`Person` for
`category='person'`, `Place` for city/country/place/university/…, else `Pod` /
`Concept` / `_default`). Section `kind` controls how it's built:

- `level_0` — deterministic one-line tagline (written last, displayed first).
- `summary` — LLM narrative summary (written after bullets; excludes contact).
- `alias` — deterministic, from `Node.aliases`.
- `bullets` — `section_tagger` selects facts, `bullet_renderer` fuses them. Every
  template includes a `general_facts` catch-all bullets section.

`SECTION_BULLET_CAPS` sets a per-section salience cap (e.g. `connection_to_user` 6,
`notes` 10, `general_facts` 10; `DEFAULT_BULLET_CAP` 8). A typical entity lands at
~15–30 bullets total. `RENDERER_CHUNK_SIZE = 40` bounds how many facts the renderer
fuses in one call. `bullet_section_names()` lists the tagger's valid destinations
(contact is one of them — it's an LLM-tagged BULLETS section, not deterministic).

## Generation — `build_card(entity_node_id)`

`app/assistant/pipelines/entity_cards_v2/builder.py`. A full, idempotent rebuild:
existing sections are wiped and rewritten. **Not** v1's contact_extractor + single
summarizer — a 5-agent pipeline plus structural walks:

1. **Gates.** `_is_user_self_entity` short-circuits the user's own entity (skip).
   Then `consumers.is_card_worthy(entity, degree=…)` (the worthiness gate — see
   below); not worthy → skip.
2. **Collect candidate facts** (`_collect_candidate_facts`). Walk 1-hop in/out
   edges; admit the far node only if `is_now_admissible`. Then a **2-hop bridge
   walk**: Entity↔Entity connections route through a State/Event/Goal/Concept
   bridge (`_BRIDGE_NODE_TYPES`), so the builder follows each bridge to its Entity
   endpoints (capped `_HOP2_MAX_ENDPOINTS_PER_BRIDGE = 5`) — this is how "Dogs"
   names Bonnie + Clyde without reading the wiki-derived `Node.description` (which
   would collapse the card↔wiki cross-check). For hub entities whose pool exceeds
   `_CARD_PREFILTER_THRESHOLD = 60`, a **nano `kg_maintenance::edge_filter`
   prefilter** (batches of 200) trims to the informative subset. A missing agent or
   a failed batch call stops the build (until 2026-10-05 it failed open and kept
   every fact); a batch answer with empty or out-of-range `keep_indices` gets one
   correction, then raises. Survivors are sorted
   by importance, then cut by the selection policy: keep iff importance ≥
   `CARD_FACT_FLOOR` OR contact-tagged, topped up to `CARD_FACT_MIN_KEEP` for
   sparse entities.
3. **Tag** each fact into a bullet section. `_tags_from_persisted` reads the
   **shared promotion-time tags** from `kg_node_section_tag` (namespace `card`)
   first; only if *no* fact has a persisted card tag does it fall back to the live
   `entity_cards::section_tagger` (`_run_section_tagger`) — transition-period path.
4. **Group + reroute.** Facts tagged `reject` or to a section not in this template,
   and facts with no card tag at all, are rerouted to `general_facts` (a fact that
   cleared the NOW + importance cuts is card-worthy by definition; the tagger only
   picks the bucket — it can't drop). A fact whose card tags were **all dropped**
   by an earlier build (drop ledger, below) is skipped, not rerouted: until
   2026-10-05 it fell through to `general_facts` and returned on every build.
5. **Render bullets** per section. Each fact carries when it was said: the first
   and last `message_timestamp` of its source messages (`kg_node_evidence`), shown
   to the renderer as `said: …`; each bullet carries the latest date of its
   sources, shown to the distiller and the summary writer as `(last said …)`.
   All three are told that when facts about the same ongoing arrangement
   disagree, the most recently said one is current (2026-10-05: a friend's card
   led with a Thursday call that had moved to Mondays). Not `Node.first_observed`:
   for nodes minted by the legacy chat import it is the import date.
   Chunked `entity_cards::bullet_renderer` fuses
   near-duplicate facts and returns each bullet's `source_fact_ids`. When a
   section exceeds its cap, `entity_cards::section_distiller` does the final
   salience cut (pick/fuse to ≤ cap). A failed distiller call aborts the build
   (until 2026-10-05 the uncapped renderer output was kept); a distiller answer
   with no bullets, more bullets than the cap, or a bullet citing no input id gets
   one correction, then raises. `_mark_section_drops` records facts the
   renderer/distiller rejected (see drop-tracking) so the next build doesn't re-pay
   to rediscover them.
6. **Deterministic + summary sections:** `_build_alias_section` (from
   `Node.aliases`), then `entity_cards::summary_writer` over the rendered bullets
   (no contact), then `_build_level_0`: only the first sentence of the summary.
   A card with no summary is a failed build: nothing to summarize, a missing
   agent or a failed call raises, and an empty summary gets one correction, then
   raises (until 2026-10-05 a summary-less card was persisted with `level_0` cut
   from the wiki-derived `Node.description`).
7. **Persist** (`_persist_card`) atomically: rewrite `card_title`/`card_aliases`
   from the node, write sections in template order, write bullets + one
   `entity_card_bullet_source_node` row per source node, stamp `last_built_at` /
   `last_full_rebuild_at`. `build_card(entity_node_id, *, activate_new=True)`:
   a card that does not exist yet is created active only when `activate_new`;
   the nightly refresh passes `False` so a new card stays inactive until the
   critic judges it.

**A failed call stops the build** (owner, 2026-10-05: any failed call in the KG
stops processing). Every LLM and DB call runs before the persist phase, so a
failure raises out of `build_card` with nothing written, and an existing card
stays as it was (stale, for the next run). A well-formed but incomplete answer
gets **one targeted correction**: the agents `edge_filter`, `section_tagger`,
`bullet_renderer`, `section_distiller` and `summary_writer` each gained a
`correction` prompt slot, and `_ask_with_one_correction` re-asks once with the
problems listed there (no or out-of-range `keep_indices`, a fact with no
`tagged_facts` entry, a bullet with no text or citing no input id, a distiller
answer empty or over the cap, an empty summary). Still incomplete, it raises.
`correction` is always passed (empty on a first call) so a reused agent never
inherits the previous call's list.

### Generator agents

| Agent | Job | Model |
|---|---|---|
| `entity_cards::section_tagger` | classify each fact into a card section or `reject` | gpt-5.6-luna |
| `entity_cards::bullet_renderer` | fuse a section's facts into terse present-tense bullets + source map | gpt-5.6-luna |
| `entity_cards::section_distiller` | salience cut when a section exceeds its cap | gpt-5.6-luna |
| `entity_cards::summary_writer` | top-of-card narrative summary from the bullets | gpt-5.6-luna |
| `entity_cards::card_critic` | 3-verdict QA judge: `pass` / `rewrite` / `veto` | gpt-5-mini |

`card_critic` asks "would an agent act differently because this card was in its
prompt?": `pass` (entity matters AND text is operational), `rewrite` (entity
matters but text is vacuous relative to its evidence), `veto` (entity doesn't
merit a card — commodities, store aisles, abstract concepts, the assistant's own
machinery). It's used by the nightly refresh as a veto gate on newly-built cards.

Agents run under the `entity_cards_v2` pipeline scope (`_scope()` →
`pipelines/entity_cards_v2/scope.yaml`).

## Shared section-tagging layer

`app/assistant/kg/section_tagging.py`. Tags are written **once at promotion time**
(by `kg_node_section_tagger` via `tag_nodes_by_id`) and read by both the card
builder and the wiki page builder — neither re-tags at projection time.

- `NodeSectionTag` (table `kg_node_section_tag`, `knowledge_graph_db_sqlite.py`):
  `(node_id, namespace, section_name)` with `tagger_version` and drop-tracking
  columns. Namespaces: `NAMESPACE_CARD = "card"`, `NAMESPACE_WIKI = "wiki"`, plus
  a `_processed` sentinel so reject/empty results aren't re-sent to the LLM every
  routine run.
- `CARD_SECTION_VOCAB` / `WIKI_SECTION_VOCAB` are the single source of truth for
  which section keys the tagger may emit per namespace; card keys mirror
  `SECTION_TEMPLATES['Person']` bullets sections.
- **Drop-tracking (try-and-mark):** `mark_facts_dropped` stamps `dropped_at` +
  `dropped_at_node_content_hash` + `dropped_by_version` on a tag the
  renderer/distiller rejected. `tag_is_active_for_build` re-admits it only when the
  node's `node_content_hash` changed OR `CARD_BUILDER_VERSION` was bumped —
  otherwise the build skips it. This is why the builder prefers persisted tags:
  the drop ledger lives on the tag row.

## Nightly routine — `entity_card_refresh`

The **only** card routine (there is no `entity_cards_pipeline` and no
`entity_card_maintenance_pipeline`; both were retired with v1). The v2 design's
KG-change-driven refresh loop sat unwired (zero callers, roster frozen since
2026-05-22) until the 2026-06-10 audit shipped it.

`configs/routines/public/entity_card_refresh.json` — daily **04:30** (after the KG
drains), `runner: function`, `function_name: entity_card_refresh`. Dispatches via
`routine_functions._lazy_entity_card_refresh` →
`pipelines/entity_cards_v2/refresh_subscriber.run_card_refresh`. Each night:

- **Stale rebuilds** (cap `max_rebuilds = 10`, highest node importance first):
  selection is stateless — a card is stale iff its entity node *or any current
  neighbor* has `updated_at` newer than `last_built_at` (`_STALE_SQL`). A per-card
  **7-day cooldown** (`cooldown_days`) damps churn: changes accumulate and trigger
  one rebuild when the cooldown expires.
- **New cards** (cap `max_new = 5`): the most-important Entities with no card,
  filtered by `is_card_worthy`. Each is built **inactive**
  (`build_card(..., activate_new=False)`), then `_judge_new_card` runs
  `card_critic` on the rendered card: `pass` or `rewrite` **activates** it; `veto`
  leaves it inactive (its row keeps the entity out of the new-card queue). A
  missing critic, a failed critic call or an answer with no verdict deletes the
  unjudged card (`_discard_unjudged_card`) and raises, so the entity is a new-card
  candidate again and the next run rebuilds and judges it.
- **Orphan deactivation** (`_ORPHAN_SQL`): active cards whose `entity_node_id` no
  longer resolves (merge losers) get `is_active = 0`.

**The first failed build or critic call stops the run** (2026-10-05): the
exception escapes to the routine runner and the remaining stale and new cards
wait for the next run. Each failure is counted in the KG failure ledger
(`app/assistant/kg/failure_ledger.py`, kind `entity_card`, one item per entity
node); after three failed runs the entity is set aside, both the stale and the
new-card selections skip it, and an escalated `processing_failure` finding puts it
in front of the owner until `failure_ledger.release` returns it. The summary dict
carries `rebuilt`, `built_new`, `vetoed_new`, `skipped` and `set_aside`.

**User-self double-gating** (commit 4bbe5b0e): `build_card` skips the user's own
entity via `_is_user_self_entity`, which matches the label against the canonical
primary-user name (`get_required_primary_user_name`) and raises when it is not
configured. Until 2026-10-05 it called `get_resource` positionally, which raised
TypeError on every build; the error was logged at DEBUG and the check returned
`False`. The refresh subscriber also filters the stale list against the same name
before picking the top `max_rebuilds`.

## Card-worthiness gate

`app/assistant/importance/consumers.is_card_worthy(node, *, degree=None)` —
Entity-only, the canonical gate. Passes if **any**:

- `locked_by_user_at` set (user-pinned), or
- `observation_count >= 2` AND `degree >= CARD_MIN_DEGREE` (15) — re-observed AND
  embedded (the conjunction filters common-noun bloat: "AC", "Lights", "Yogurt"
  rack up observations from ordinary English without graph presence), or
- `observation_count == 1` AND `degree >= 10` (one deep first-mention window), or
- `effective_importance(node) >= CARD_HIGH_IMPORTANCE_FLOOR` (7.0) — the graph says
  it's defining even if sparse.

Companion thresholds live alongside: `CARD_FACT_FLOOR = 0.6` (per-fact importance
floor in collection) and `CARD_FACT_MIN_KEEP = 50` (sparse-entity safety net),
with a **contact carve-out** — facts the section-tagger marked `contact` bypass
the floor, because the source-perspective rater systematically undervalues "X's
phone number" (it matters to whoever wants to reach X, not to X).

## KG link binding + lifecycle

`entity_card_v2.entity_node_id` is the structural link: FK to `kg_node_metadata.id`
with `ondelete='CASCADE'`, nullable (NULL = non-kg card), unique. The
`uq_entity_card_v2_active_title` partial unique index keeps at most one active card
per `card_title`.

- **Node content changes** — the nightly refresh sees a newer neighbor/entity
  `updated_at` and rebuilds (after cooldown). `regenerate_entity_card` forces it on
  demand.
- **Node deleted / merged away** — the orphan sweep deactivates the card
  (`is_active = 0`); the cascade FK also removes child rows if the node row itself
  is deleted.
- **Node re-labelled** — `_persist_card` rewrites `card_title` from the node on
  every build, so the rename propagates.

## Prompt-injection renderer

`entity_card_v2.render_v2_card_for_prompt_injection_level(session, entity_name, *, level=1, sections=None)`
looks a card up by `card_title` (no KG detour, so non-kg cards are first-class)
and renders to text:

- **L0** — bare one-liner (the `level_0` section's `intro_text`).
- **L1** — header + summary.
- **L2** — L1 + "Facts" (what_they_do / where_they_are / notes / characteristics / …).
- **L3** — L2 + contact (rendered from the contact section's bullets).
- **L4** — L3 + relationships (connection_to_user / current_connections / …) + aliases.
- `sections=[…]` overrides `level`: render exactly those sections in order (e.g.
  `personal_admin` wants `level_0` + `contact` only, not the L3 superset).

It logs a hard ERROR and falls back to most-recent if it ever sees plural active
matches for a title (the partial unique index should make that impossible).

### Injection path

`entity_management/entity_cards.py` is now a **bridge shim** — the v1 ORM and its
helpers are gone. It exports `get_entity_card_for_prompt_injection_level` (thin
wrapper over the v2 renderer) and a no-op `track_entity_card_usage_best_effort`
(v1's usage table is gone). Two consumers go through it:

- `agent_runtime/services/entity_injector.py` (`EntityInjector`) — the prompt-build
  path. It renders the agent's prompt once with entity keys blanked, detects
  entities in the *rendered* text (not raw context, to avoid false positives from
  JSON-dumped blobs), seeds room-pinned/allowed entities, then fills keys like
  `entity_card` / `entity_summary` / `entity_level_0` at the configured level
  (`entity_card_level`, default 1) or `entity_card_sections` override.
- `entity_management/entity_card_injector.py` (`EntityCardInjector`) — the chat /
  team-call lexical-match injector. Tokenizes text (handling possessives/aliases),
  matches against the in-memory `EntityCatalog`, dedupes against chat history, and
  prepends `[Entity Context - Name]:` blocks via the same v2 renderer.

## UI — `/entity-cards-v2`

`app/routes/entity_cards_v2.py` (`entity_cards_v2.html`). The v1 editor / admin /
maintenance routes (`entity_cards_editor`, `entity_cards_admin`,
`entity_card_maintenance`) **no longer exist**.

- `GET /entity-cards-v2` — viewer page.
- `GET /api/entity-cards-v2` — list active cards (title, type, section/bullet counts).
- `GET /api/entity-cards-v2/<id>` — one card's full sections + bullets.
- `POST /api/entity-cards-v2` — create a **non-kg** card (`entity_type='Manual'`,
  unique title; stored as `level_0` + `summary` sections).
- `PUT /api/entity-cards-v2/<id>` — edit a non-kg card. **kg cards → 403** (driven
  by the pipeline; per-field editing is phase 2).
- `DELETE /api/entity-cards-v2/<id>` — delete a non-kg card. **kg cards → 403**.

CRUD reloads the `EntityCatalog` so detection picks up the change on the next
prompt build. kg cards (`entity_node_id IS NOT NULL`) are read-only here.

## Agent tool — `regenerate_entity_card`

`app/assistant/lib/tools/regenerate_entity_card/`. A manager-callable tool that
resolves `entity_label` → KG node (preferring `category='person'`) and calls
`build_card(entity_node_id)`. Use after KG mutations that change facts the card
summarized (closed a State, renamed a node, added a fact). Synchronous; rebuilds
all sections. `min_authority`/`approval_min_authority` 95, `risk_level` low,
`side_effects` write. Latency scales with neighborhood size (~30–90s typical,
~5–10 min for the user's own card — though that one is gated out of building).

## Relationship to the wiki

| | Entity Card | Wiki page (`11_WIKI_GENERATOR.md`) |
|---|---|---|
| Temporal stance | NOW snapshot | historic narrative |
| Form | section/bullet DB rows | Markdown file (frontmatter + prose) |
| Target consumer | agent prompts (lexical-match injection) | human reader + agents hydrating full neighborhoods |
| Storage | `entity_card_v2` + section/bullet tables | files in the `EmiWiki` vault |
| Refresh | nightly `entity_card_refresh` (whole-card rebuilds) | nightly section refresh |
| Shared upstream | promotion-time `kg_node_section_tag` (`namespace='card'`) | same tagger (`namespace='wiki'`) |

The card builder must **not** read `Node.description` (it's wiki-derived); it walks
KG structure (the 2-hop bridge walk) instead, keeping the card and wiki as
independent cross-checkable projections.

## Key files

| Path | Role |
|---|---|
| `app/assistant/entity_management/entity_card_v2.py` | ORM (4 tables), templates, caps, NOW filter, prompt-injection renderer |
| `app/assistant/entity_management/entity_cards.py` | v1 bridge shim (renderer wrapper + no-op usage) |
| `app/assistant/pipelines/entity_cards_v2/builder.py` | `build_card`, fact collection + bridge walk + prefilter, one-correction helper |
| `app/assistant/pipelines/entity_cards_v2/refresh_subscriber.py` | nightly `run_card_refresh` (stale / new / orphan, new cards built inactive and judged by the critic, user-self filter, failure ledger) |
| `app/assistant/kg/section_tagging.py` | shared promotion-time tagging + drop-tracking |
| `app/assistant/kg/db/knowledge_graph_db_sqlite.py` | `NodeSectionTag` model |
| `app/assistant/importance/consumers.py` | `is_card_worthy` + card thresholds |
| `app/assistant/agents/entity_cards/{section_tagger,bullet_renderer,section_distiller,summary_writer,card_critic}/` | the 5 generator/QA agents |
| `app/assistant/agent_runtime/services/entity_injector.py` | prompt-build entity detection + leveled rendering |
| `app/assistant/entity_management/entity_card_injector.py` | chat/team lexical-match injector |
| `app/routes/entity_cards_v2.py` | viewer + non-kg CRUD |
| `configs/routines/public/entity_card_refresh.json` | nightly routine config (04:30) |
| `app/assistant/lib/tools/regenerate_entity_card/` | on-demand single-card regen tool |

## How to build / regenerate / debug

**Build one card** (standalone — bootstrap DI first via
`import app.assistant.tests.test_setup`):
```bash
.venv\Scripts\python.exe -c "from app.assistant.pipelines.entity_cards_v2.builder import build_card; from app.assistant.kg.db.knowledge_graph_db_sqlite import Node; from app.models.base import get_session; s=get_session(); nid=s.query(Node).filter(Node.label=='a family member', Node.node_type=='Entity').first().id; s.close(); print(build_card(nid))"
```

**Force-regen by label** — the `regenerate_entity_card` tool, or `build_card(node_id)`.

**Run the nightly refresh on demand**:
```bash
.venv\Scripts\python.exe -c "from app.assistant.pipelines.entity_cards_v2.refresh_subscriber import run_card_refresh; print(run_card_refresh(dry_run=True))"
```
`dry_run=True` returns the stale/new/orphan selection without writing.

**Card empty for a real entity** — check the NOW filter (all neighbors closed →
nothing admitted), the worthiness gate (`is_card_worthy`), and whether persisted
`card` tags exist for its neighbor nodes (else it falls back to the live tagger).
The `general_facts` reroute should keep meaningful edges from vanishing.

**Card not injecting** — confirm `is_active = 1`, the `EntityCatalog` has the title/
alias (CRUD reloads it; restart Flask if rows were hand-edited), and the entity
name actually appears in the agent's *rendered* prompt (detection scans rendered
text, not raw context).
