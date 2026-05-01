# `/me` — the personal lens

Status: design lock for v0. Companion to `cli_emi.md`.

## What this is

An interactive personal-graph navigator. The graph is the substrate; the user explores by asking questions and the system shows bounded, relevance-ranked answers. Replaces the current `/kg-visualizer` page entirely. Built clean-room in `app/me/` — no imports from `app/graph_visualizer/` or `app/routes/kg_visualizer.py`.

It is **not** a wiki replacement. The wiki gives biography in prose; the lens gives navigation by graph.

It is **not** the curatorial / admin tool. That's a separate future surface (`/kg-admin`). The lens is mostly read-only — clicks on suspicious data flag for review or hand off to admin mode, never mutate inline.

## Three principles

1. **Question-driven, not browse-driven.** Every view is the result of a query. Default landing: seed=Jukka, fresh subgraph rendered at open.
2. **Always bounded.** Soft cap at 50 visible nodes by personalized PageRank score. Beyond that fade and shrink; hard cap at 100 to keep the renderer happy.
3. **Discoverable by zoom, click, and shift-click.** All data is reachable, just not all at once. Click a node → opens its wiki page (in a side panel). Shift-click a node → it joins the seed set; the view rebalances. Zoom in → faded ranks 51–100 fade in.

## The core primitive: personalized PageRank with a seed set

Every visible subgraph is computed as: **personalized PageRank seeded by the active seed set, top-K by score**. This generalizes everything:

- Empty seeds (impossible — default is `[jukka_node_id]`).
- One seed (Jukka) → "what's important to me right now."
- Two seeds (Jukka + Katy) → "what's important between us."
- N seeds → "what's important across this group."

**Shift-click** a visible node → add it to the seed set, recompute, redraw. The graph stays sparse because the cap stays at 50. Removing a seed (chip × button) recomputes back. Plain click opens the wiki page for the node — see "Wiki integration" below.

NetworkX `pagerank(personalization={seed_id: 1.0 for seed in seeds})` does the math directly. For 4,000 nodes it runs sub-second; we cache by `(sorted(seeds), time_filter_hash)`.

### Time as a global filter

A toggle and an optional date range, applied before PageRank runs:

- **Currently true** (default): edges with `valid_during=ongoing` or no `end_date`. State-of-the-world right now. Cleaner.
- **Lifetime**: all edges ever.
- **Date range**: edges whose `[start_date, end_date]` overlap the picked window. Slider or text input.

The PageRank runs on the time-filtered subgraph, so "important between 2022 and 2024" surfaces era-specific nodes.

### Edge inclusion

If two non-seed visible nodes have an edge between them (e.g. Annika ↔ Peter, when seeds are Jukka + Katy), show it. The structure between visible nodes is part of the value. Cap doesn't apply to edges — only nodes.

## UX shape (v0)

No top bar.

```
┌────────────────────────────────────────────────────┐
│  [seed chips: Jukka × | Katy ×]                    │  ← hovers above canvas
│                                                    │
│              [graph canvas, full viewport]         │
│                                                    │
│                                                    │
│                                                    │
│                                                    │
├────────────────────────────────────────────────────┤
│  [chat input: "Ask anything... e.g. 'people I    ] │
│  [                  worked with at Acme'         ] │
└────────────────────────────────────────────────────┘
```

Interactions:
- **Hover** node → quick-look popover with photo + one-liner + 3 action buttons.
- **Click** node → opens wiki side panel (slides in from right, ~40% viewport).
- **Shift-click** node → adds it to the seed set, view rebalances.
- **Cmd-click** seed-set node OR click × on its chip → removes from seed set.
- **Drag** seed chip → reorder for visual grouping (no semantic effect; just user comfort).
- **Wheel / pinch** → zoom. Zoom-in fades in ranks 51–100 if any.

## Visual design

- 2D only. No 3D for v0. Drop `react-force-graph-3d`.
- **Visual hierarchy: entities dominate, states subdue.** Entities (Person, Place, Organization, etc.) are the focal points. State nodes (Marriage, Career, Residence, etc.) are connective tissue — small, neutral, easy to skim past visually. The KG represents n-ary relationships as state vertices for storage, but the lens reads them as edge-decorations.

### Sizing

- **Entities (Person, Place, Organization, Product, Concept):** large. ~50px diameter (or equivalent footprint). Photos, prominent labels.
- **Events:** medium. ~30px. Chip/tag with date stamp and label.
- **Goals:** medium. ~30px. Arrow/target shape.
- **States:** small. ~16px chip with a single-word type label ("marriage", "career"), neutral gray. Positioned along the line between the entities they connect, so they read as intermediating annotations, not first-class subjects.

### Per-type treatment

- **Person** (entity, large): circular photo (or initials avatar) + name below. Border ring thickness = personalized-PageRank score under the active seed set. Selected/seed nodes get a brighter accent border.
- **Place** (entity, large): rounded square with a map-pin icon, label below. Photo if available.
- **Organization / Product** (entity, large): rounded square with logo if available, label below.
- **Event** (medium): chip with date stamp + short label. Color tinted by recency.
- **Goal** (medium): arrow/target shape + label.
- **State** (small, subdued): tiny chip in muted gray with single-word type label. No photo, no border ring. Hovers to expand to its full label and date range.

### Edges

Thin lines, weight by importance/confidence. No edge labels on render — labels appear on edge hover only. State nodes positioned mid-edge effectively act as the edge label most of the time.

## Node photos

Source priority:
1. Entity-card profile image if set (`materialize_profile_image_for_vault` already knows this).
2. Resource identity image for the user/known principals.
3. Most recent image pod tagged with the entity's name/aliases.
4. Fallback: initials avatar (first letters of label, hashed-color background).

Photo URLs served from a small endpoint `/api/me/photo/<node_id>`.

## Chat input → query

The chat input is the primary control beyond click. v0 supports a small set of templates:

- `connected to <X>` — adds X to seed set.
- `<X>'s family` / `family of <X>` — seed = X, filter to family edge types.
- `events between <D1> and <D2>` — apply date range.
- `places of <X>` / `places I've been` — seed = X (or me), filter to Location edges.
- `everything about <X>` — replace seeds with [X].
- `clear` / `reset` — reset to default (seeds=[jukka]).

Implementation v0: hand-rolled regex + entity name fuzzy match against `kg_node_metadata.label/aliases`. If it doesn't match a template, fall back to an LLM call (small/mini tier) that picks a template + fills params. Reasonable cost (~cents per query). The chat shows a one-line summary of what it understood ("focusing on Katy's family between 2022-2024") so user has feedback.

Out of scope for v0: full natural-language-to-Cypher. That's a v1+ ambition.

## Wiki integration

**Plain click on a node opens its wiki page** — the lens is the entry point, the wiki is the content. Two interaction modes:

- **Side panel (default):** wiki page renders in a slide-in panel from the right, ~40% viewport. Graph stays visible on the left. Closes on Esc or back-button. Lets the user keep their place in the graph while reading.
- **Full open:** "open full" button in the side panel header opens the wiki at `/wiki/<entity_label>` in a new tab.

Quick-look popover anchored to the node — small, photo + one-liner + 3 actions (open wiki / add to focus / flag) — appears on **hover**, not click. Click commits to opening the wiki.

For nodes that don't have a wiki page yet (e.g., low-importance entities, fresh extractions), the panel falls back to entity_card data + a "request wiki" button that triggers the wiki_generator's nightly path on demand.

## Read-only stance

The lens does not mutate. Suspicious-data affordances live in the wiki side panel and quick-look popover:
- "Flag this" → writes a `kg_maintenance_finding` row for the curatorial workflow to pick up.
- "Open in admin" → hands off to the future admin surface (or, for v0, falls back to `/kg-maintenance`).

This keeps the lens calm and aesthetic. Curation is its own product.

## Backend endpoints (new, in `app/me/api.py`)

```
GET  /api/me/seed-graph?seeds=ID,ID,ID&time_mode=current|lifetime|range&time_from=&time_to=&limit=50
     → { nodes: [...], edges: [...], pagerank: {id: score} }

GET  /api/me/node/<id>
     → { node, photo_url, summary, edges_in, edges_out }

GET  /api/me/photo/<id>
     → image bytes (304 cache friendly)

POST /api/me/parse-query    body: { text }
     → { template, seeds, time_mode, time_from, time_to, summary }

POST /api/me/flag           body: { node_id?, edge_id?, reason }
     → { finding_id }
```

WebSocket events (subscribed by the lens, emitted by the audited mutation pipeline):
- `lens.graph.node.upserted`, `lens.graph.node.deleted`
- `lens.graph.edge.upserted`, `lens.graph.edge.deleted`

The lens applies these in-place to the current view if visible; otherwise ignores.

## Tech stack

Backend:
- New module `app/me/`. Plain Flask blueprint, registered in `app/routes/__init__.py`.
- `networkx` for PageRank — already a dependency.
- Reads `kg_node_metadata` and `kg_edge_metadata` directly via existing SQLAlchemy models. Reuses `app.models.db_manager` for sessions. **No imports from `app/graph_visualizer/`.**

Frontend:
- Vite + React 18 + TypeScript.
- Tailwind CSS.
- TanStack Query for data fetching.
- React Router (likely overkill for v0, but cheap).
- `react-force-graph-2d` for graph rendering.
- `socket.io-client` for live updates.
- shadcn/ui or Radix UI primitives for the chat input + side panels (clean accessible defaults).

Build: `npm run build` produces `app/me/frontend/dist/`. Flask serves it at `/me/...`. The build directory is `.gitignore`d — built in dev or CI, not committed.

## Phasing within v0

- **Day 1–2:** backend skeleton, `/api/me/seed-graph` working, tested against live KG.
- **Day 3–4:** Vite scaffold, basic 2D graph render with hardcoded seeds, color/shape per node_type, photo wiring.
- **Day 5–6:** chat input → regex query templates → seed mutation, click-to-expand additive seeds, time-mode toggle.
- **Day 7–8:** quick-look popover, "flag this", styling polish, photo fallbacks.
- **Day 9–10:** WebSocket live updates wired (producer-side broadcasts only need to fire when the audited mutators commit).

Total: ~2 weeks for a feeling-real v0.

## What we explicitly defer to v1+

- 3D mode.
- Multi-layout (timeline, map, hierarchical). The graph is force-directed only in v0.
- LLM-driven natural language parsing beyond the regex+template fallback.
- Saved views / shareable URLs.
- Time-travel (graph as of <date>).
- Inline mutation. Curation stays in the future admin surface.
- Diff views ("what changed in the last hour").

## Out-of-scope for the project entirely

- Multi-user / shared views.
- Public/sharable graph snapshots.

## Acceptance criteria for v0 ship

1. Open `/me`, see Jukka centered with top-50 personalized-PageRank neighbors.
2. Click any node → it joins the seed set; view rebalances within 1s.
3. Type "Annika's family" → seed=[Annika], family-edge filter applied.
4. Type "events between 2022 and 2024" → time-range filter applied to current seeds.
5. Toggle currently-true vs lifetime → graph updates.
6. Click flag → finding saved.
7. Make a KG mutation in kg-dev console → lens redraws within 2s.
