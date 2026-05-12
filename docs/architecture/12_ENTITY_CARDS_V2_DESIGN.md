# Entity Cards v2 — Section-tagged, Bullet-incremental, NOW-snapshot

**Status:** design — not yet implemented.
**Supersedes:** `12_ENTITY_CARDS.md` (v1) when shipped.
**Drafted:** 2026-05-10, post-nuke redesign window.

## Motivation

Entity cards v1 generates each card as a single prose blob via one LLM
call on the entity's neighborhood. Three problems:

1. **Whole-card regeneration on any change.** A phone number tweak triggers
   a full ~400-word LLM re-write. Wasteful in cost and prone to drift
   (each regeneration risks reshuffling unrelated bullets).
2. **No temporal stance.** v1 cards mix active state ("lives in Irvine"),
   closed state ("worked at X in 2015"), and past events ("traveled to
   Japan 2018"). The "what's true NOW" signal is buried.
3. **Opaque bullets.** v1's `key_facts` are LLM-paraphrased; no
   back-pointer to the source KG node, so investigator can't verify or
   refresh per-fact.

v2 follows the wiki page generator's section-tag-then-fill pattern but
extends it to per-bullet incremental refresh.

## Three-layer mental model

| Layer        | Temporal stance                | Contains                                                                                  | Refresh cadence                                          |
|--------------|--------------------------------|-------------------------------------------------------------------------------------------|----------------------------------------------------------|
| KG           | All-time                       | Every observation, open and closed states, all events                                     | On every ingest                                          |
| Entity card  | NOW snapshot                   | Active states, definitional bio, active goals, current connections                        | When an active fact changes                              |
| Wiki         | Historic narrative             | Past events, closed-era states, biographical timeline, achievements                       | When a state closes / event added                        |

Entity cards are a **NOW filter** over the KG. Wiki is a chronological
projection. Both consume from KG; both have their own incremental
refresh.

## Card structure

Per Person card (other entity types adapt the sections):

```
SECTION                      NOW-filter inclusion rule
─────────────────────────────────────────────────────────────────────
Identity                     Always include. Names, aliases, ID props
                             (email, phone, etc.) regardless of decay.

Connection to <user>         Active relationship state (no end_date).

Where they are               Active residence + workplace.

What they do                 Active occupation, ongoing projects.

Notes about them             Active preferences, traits, ongoing context.

Current connections          Other entities currently interacted with
                             (filtered by recent observation count).
```

A bullet only enters the card if the NOW filter admits it.
Closed states and past one-off events go to the wiki, not the card.

## NOW filter (canonical rule)

For a State / Event / Goal node connected to the entity:

```python
def is_now_admissible(node) -> bool:
    if node.locked_by_user_at is not None:
        return True   # user-locked overrides everything
    if node.node_type == "State":
        return node.end_date is None or node.end_date > NOW
    if node.node_type == "Goal":
        return node.goal_status in ("active", "in_progress", None)
    if node.node_type == "Event":
        # Only definitional events go on the card. Wedding, birth,
        # graduation, employment-start, marriage-start.
        return node.category in DEFINITIONAL_EVENT_CATEGORIES
    if node.node_type == "Property":
        return True   # properties are timeless attributes
    return False  # Entity / Concept are link targets, not bullets
```

`DEFINITIONAL_EVENT_CATEGORIES` is a small allowlist: `birth`, `wedding`,
`marriage_start`, `death`, `graduation`, `move`, `hire`, etc. — events
that anchor the present even though they happened in the past.

## Storage schema

Three new tables:

### `entity_card` (replaces v1 `entity_cards`)

```sql
CREATE TABLE entity_card (
    id                   TEXT PRIMARY KEY,
    entity_node_id       TEXT NOT NULL,        -- canonical KG node
    entity_type          TEXT NOT NULL,
    is_active            BOOLEAN NOT NULL DEFAULT 1,
    last_built_at        TIMESTAMP,
    last_full_rebuild_at TIMESTAMP,
    FOREIGN KEY (entity_node_id) REFERENCES kg_node_metadata(id)
);
```

### `entity_card_section`

```sql
CREATE TABLE entity_card_section (
    id              TEXT PRIMARY KEY,
    card_id         TEXT NOT NULL,
    section_name    TEXT NOT NULL,       -- 'identity', 'connection', etc.
    position        INTEGER NOT NULL,
    intro_text      TEXT,                -- optional 1-2 sentence section intro
    intro_source_hash TEXT,              -- hash of bullets that produced the intro
    UNIQUE (card_id, section_name),
    FOREIGN KEY (card_id) REFERENCES entity_card(id) ON DELETE CASCADE
);
```

### `entity_card_bullet`

```sql
CREATE TABLE entity_card_bullet (
    id                  TEXT PRIMARY KEY,
    section_id          TEXT NOT NULL,
    position            INTEGER NOT NULL,
    bullet_text         TEXT NOT NULL,
    source_node_ids     JSON NOT NULL,      -- array of KG node ids feeding this bullet
    source_edge_ids     JSON NOT NULL,      -- array of KG edge ids feeding this bullet
    source_hash         TEXT NOT NULL,      -- hash of source content (drives diff)
    confidence_tier     TEXT,               -- inherits from weakest source tier
    generated_at        TIMESTAMP NOT NULL,
    FOREIGN KEY (section_id) REFERENCES entity_card_section(id) ON DELETE CASCADE
);

CREATE INDEX ix_card_bullet_source_node ON entity_card_bullet (
    -- denormalized to a per-node-id row for fast reverse lookup;
    -- implementation: a sibling table entity_card_bullet_source_node
    -- (bullet_id, node_id) with INDEX on node_id.
);
```

A sibling lookup table for fast "which bullets reference this node":

```sql
CREATE TABLE entity_card_bullet_source_node (
    bullet_id   TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    PRIMARY KEY (bullet_id, node_id),
    FOREIGN KEY (bullet_id) REFERENCES entity_card_bullet(id) ON DELETE CASCADE
);
CREATE INDEX ix_bullet_source_node_node_id ON entity_card_bullet_source_node(node_id);
```

This lets `WHERE node_id = ?` return the affected bullets in O(log n).

## Agents

| Agent                            | Job                                                                   | Model       |
|----------------------------------|-----------------------------------------------------------------------|-------------|
| `entity_cards::section_tagger`   | Classify an edge/neighbor fact into a card section, OR reject (wiki)  | small-tier  |
| `entity_cards::bullet_renderer`  | Render ONE bullet from one set of source nodes                        | small-tier  |
| `entity_cards::section_intro`    | Render 1-2 sentence section intro from N bullets (optional)           | small-tier  |
| `entity_cards::card_summary`     | Top-of-card narrative summary (optional, only on full rebuild)        | mid-tier    |

Compared to v1's single `entity_card_summarizer`, this is **4 cheaper
small-tier agents** instead of 1 expensive mid-tier agent. Total cost
per card on full build is comparable to v1; on incremental refresh it's
10-100× cheaper.

## Refresh flow

### Full rebuild (new entity reaches Group A pagerank threshold)

```
1. Identify entity's KG neighborhood (active edges via NOW filter)
2. For each connected fact:
     tag = section_tagger(fact, entity)
     if tag == 'reject':
         skip (wiki may pick it up)
     else:
         buffer[tag].append(fact)
3. For each tag with non-empty buffer:
     for each fact in buffer[tag]:
         bullet = bullet_renderer(fact)
         INSERT entity_card_bullet
4. Render section intros (optional)
5. Render card summary (optional)
6. Mark entity_card.last_full_rebuild_at = NOW
```

### Incremental refresh (KG fact changed)

Triggered by `kg_revision_log` insert OR by a scheduled diff pass.

```
On change to node_id X:
  1. affected_bullets = SELECT bullet_id FROM entity_card_bullet_source_node
                        WHERE node_id = X
  2. For each affected bullet:
       sources = fetch all source_node_ids
       if NOW filter rejects current state (e.g., state just closed):
           DELETE bullet
           bubble up: maybe regenerate section intro
       else:
           new_text = bullet_renderer(sources)
           UPDATE entity_card_bullet SET bullet_text = new_text,
                                          source_hash = <new>
  3. If section composition changed (bullets added/removed):
       regenerate section intro
  4. Card-summary regen ONLY if a defined trigger fires
     (e.g., relationship type changed, address country changed)
```

### State-close event (active state ends → wiki territory)

```
1. NOW filter starts rejecting affected bullets
2. Their card bullets get DELETED (state has moved to wiki)
3. Wiki refresh enqueued (separate flow)
```

## Section-tagger prompt sketch

```
You are classifying a single KG fact into a section of an entity card
about <entity_label> (<entity_type>).

The fact:
  - source node: <node.label> (<node_type>, category: <category>)
  - sentence: "<fact.sentence>"
  - active: <yes/no, based on end_date/goal_status>
  - definitional: <yes if event in DEFINITIONAL_EVENT_CATEGORIES>

Available sections for this entity_type:
  identity, connection_to_user, where_they_are, what_they_do, notes, current_connections, reject

Rules:
- If the fact is a CLOSED state or PAST one-off event (not definitional),
  classify as 'reject' — the wiki handles historic content.
- Properties (email, phone, etc.) → 'identity'
- Active residence / workplace / location → 'where_they_are'
- Active relationship to <user_name> → 'connection_to_user'
- Active occupation / ongoing project → 'what_they_do'
- Trait, preference, ongoing context → 'notes'
- Other active entities the subject interacts with → 'current_connections'

Output: {section: <name>, confidence: high|medium|low, reason: <short>}
```

## Bullet-renderer prompt sketch

```
Write ONE bullet for an entity card. Keep it terse, present-tense,
factual. No prose flourish.

Subject: <entity_label>
Section: <section_name>
Source facts (will be joined into a single bullet):
  - <fact 1>
  - <fact 2>
  - ...

Style:
- 1 sentence, <= 20 words.
- Present tense. "Lives in Irvine" not "is living in Irvine".
- Anchored to the subject. "Works as data scientist at Seyfarth"
  not "is currently employed as a data scientist..."
- For relationships, first-person framing when subject is a person
  in <user_name>'s life: "my wife", "the kids" — not "Jukka's wife".

Output: just the bullet text.
```

## Refresh hooks

`kg_revision_log` is the canonical change feed. A `card_refresh_subscriber`
service reads new rows and:

1. Extracts changed `node_id` / `edge_id`
2. Joins to `entity_card_bullet_source_node` to find affected bullets
3. Re-renders only those (cost: ~$0.001-0.005 per bullet)

The per-bullet refresh path IS the per-month-batch rebuild path. After
each month of pipeline backfill, the same subscriber drains the new
revision_log rows and updates only the affected bullets. There's no
separate "do a big rebuild at the end" mode — the incremental design
handles bulk just as cheaply as ongoing single-fact updates.

## Cost projection

| Operation                              | v1 cost            | v2 cost                  |
|----------------------------------------|--------------------|--------------------------|
| Full card build (per entity)           | ~$0.20             | ~$0.20 (similar)         |
| Single fact change (e.g., phone)       | ~$0.20             | ~$0.005                  |
| Section refresh (5 bullets changed)    | ~$0.20             | ~$0.02                   |
| State close (one bullet removed)       | ~$0.20             | $0.00 (delete only)      |
| 100 cards, 1 fact each changes/day     | ~$20/day           | ~$0.50/day               |

The 40× reduction on incremental refresh is the v2 win.

## Compatibility / migration

KG is empty post-nuke; entity_cards table is empty. v2 can be built
without migration. When ready, drop v1 `entity_cards` table, create the
three v2 tables, and only the new builder runs going forward.

## Open questions

1. **Section intro** — necessary at all? Could omit, list bullets directly.
   Wiki sections have intros; cards might not need them. **Defer until prompt experiments show value.**

2. **Card summary** — top-of-card paragraph. Useful for LLM context
   injection (resolver / extractor can read the summary instead of all
   bullets). But forces regen logic to handle summary diff. **Lean
   toward keeping; trigger regen only when sections add/remove bullets,
   not on every bullet change.**

3. **Definitional events** — exact category allowlist needs curation.
   First-pass: `birth`, `wedding`, `marriage_start`, `death`, `move`,
   `graduation`, `hire`. Iterate from real card output.

4. **Card for Jukka himself** — special structure? Probably same shape
   but with sections renamed (the "Connection to user" section makes no
   sense). Could template: Person-Other vs Person-Self.

5. **Confidence_tier rollup** — should card bullet inherit weakest tier
   from sources (provisional poisons), or strongest (axiom dominates)?
   **Lean weakest** — investigator should know if a bullet is built on
   provisional data.

6. **Section ordering** — currently positional via `position` column.
   Either hard-coded per entity_type, or LLM-decided per card.
   **Hard-code initially**, can revisit if cards feel formulaic.

## Next steps

1. Get this design reviewed (you're reading it).
2. Build the three tables + `entity_card_bullet_source_node` lookup.
3. Build `entity_cards::section_tagger` + `bullet_renderer` agents.
4. Build the orchestrator. Start with full-rebuild path.
5. Then build the `kg_revision_log` subscriber for incremental refresh.
6. Migrate `kg_entity_card_pipeline` to use the v2 builder.
7. Drop v1 `entity_cards` table; supersede this doc into `12_ENTITY_CARDS.md`.

Per-month incremental builds are explicitly supported — the refresh
subscriber drains revision_log batches efficiently regardless of size.
No need to wait til all legacy is processed before generating cards.
A reasonable cadence: after each month's pipeline batch + promote
completes, kick the card refresh subscriber. The graph keeps growing,
the cards keep tracking the NOW snapshot.
