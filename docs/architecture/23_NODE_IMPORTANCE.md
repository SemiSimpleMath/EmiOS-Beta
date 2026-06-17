# Node and edge importance

How the KG decides which nodes/edges matter to the user, who scores them,
when, and how the score flows. This is the signal behind the personalized
lens, card-worthiness, wiki growth, timeline prominence, and finding
priority.

The model is **one LLM rater (edges only) + two deterministic
derivations (nodes).** It shipped 2026-05-12 (commit `f3135cb2`) and
replaced an earlier per-node LLM Entity rater. Everything lives in
`app/assistant/importance/`.

## What gets rated

| Subject | How | Where | Scale |
|---|---|---|---|
| **kg_edge** rows | LLM, `me::edge_importance_rater`, source's perspective | `scoring.regenerate_edge_importance` | 0–10 → `kg_edge_metadata.importance` |
| **Entity / Concept** nodes | derived: `MAX(adjacent edge.importance)` | `scoring.regenerate_entity_importance` | 0–10 → `kg_node_metadata.importance` |
| **State / Event / Goal / Property** nodes | derived: `MAX(other_end.importance × edge.importance / 10)` | `scoring.regenerate_state_importance` | 0–10 → `kg_node_metadata.importance` |

The single source-of-truth module is `app/assistant/importance/scoring.py`
(the three `regenerate_*` writers, each with a long WHY docstring that is
the authoritative spec). Consumer-side thresholds and gates live in
`app/assistant/importance/consumers.py`.

> History: the edge rater used to live at `app/assistant/kg/edge_importance.py`
> and the node deriver at `app/me/importance.py`; both were deleted and
> folded into `importance/scoring.py` on 2026-05-17. The deprecated LLM
> Entity rater (`regenerate_importance`) was removed 2026-05-12. The
> `me::importance_rater` **agent directory still exists on disk but is
> orphaned** — no live code creates it (only stale comments in
> `kg/proposal_promoter.py` still name it).

## The pipeline

`routine_manager/routine_functions.py::_lazy_kg_importance_rater`
(registered as the `kg_importance_rater` routine) runs the whole chain.
Config: `configs/routines/public/kg_importance_rater.json` — interval,
`min_interval_seconds = 1800` (~30 min), `max_run_seconds = 900`,
`batch_size_edges = 50`, `max_edges_per_run = 400`. Gated by the
`kg_edge_importance_rating` subsystem flag.

One scope is built at the routine entry and threaded into the two LLM
steps; the pure-DB derivations take none. Steps, in order:

1. **Rate edges** — `regenerate_edge_importance(only_unrated=True, max_edges=400)`.
   The single LLM input. Batches of 50 to `me::edge_importance_rater`.
2. **Derive Entity / Concept** — `regenerate_entity_importance(only_unrated=False)`.
   `MAX` over each node's adjacent edge importances.
3. **Derive State / Event / Goal / Property** — `regenerate_state_importance(only_unrated=False)`.
   `MAX(src_imp × edge_imp / 10)` over connecting edges.
4. **Section tagging** — `kg/section_tagging.backfill_untagged_nodes(limit=60)`,
   which now sees real importance values for its source-entity gate.

The order is load-bearing: step 3 reads the Entity importances written
by step 2; step 4 reads both. The deferred-dependency chain is
`promote (NULL importance) → rate edges → derive nodes → tag → card/wiki dirty-sweep`.

Per-run caps (`max_edges`, `max_tags`) pair with `only_unrated` /
untagged shrinking selectors so a bulk re-extraction converges over
several runs instead of becoming one graph-wide LLM grind.

## Edge importance — the only LLM rating

`me::edge_importance_rater` (`gpt-5.4-mini`, `agents/me/edge_importance_rater/`)
rates each edge **from the source node's perspective** on 0–10:
"how important is this relationship to the source?" `the user --addressee--> the assistant`
is rated by how much it matters to *the user*, not to the assistant, not in the
abstract. The number is **single-anchored** — only the source's view is
stored; a consumer wanting the other side must walk the reverse edge.

Two disciplines from the prompt (`prompts/system.j2`):

1. **Reason before score.** The agent writes a 1–2 sentence `reason`
   restating what the EDGE SENTENCE describes, why it matters to the
   source, and which band fits — *before* the number. Forcing the reason
   first keeps the score honest.
2. **Read the EDGE SENTENCE, not the `relationship_type`.** `has_state`
   spans "is deceased" (9–10) to "has email address" (1–2). The label is
   a coarse extraction bucket; the sentence is what's scored. The prompt
   block all-caps the edge sentence to anchor the model on it
   (`scoring._build_edge_block`).

The framing in the prompt: **importance is the blast radius of getting
it wrong** — score by how much downstream behavior breaks if a consumer
misreads the edge, not by how interesting the fact feels.

Calibration anchors (integer bands, `.5` between; from the source's
perspective — most edges connect an Entity source to a State/Event/Goal):

| Band | Examples (EDGE SENTENCE) |
|---|---|
| **10** | "married to the user's partner since 2003"; "the user's partner is a family member's mother"; "father is dying of cancer" — most defining relationships/states of a life |
| **9** | family-tier bonds, primary career anchor: "two beloved dogs walked daily"; "senior engineer at Google — primary career"; "sister, talk monthly" |
| **8** | in-laws, formative mentors, identity-defining commitments, life-goals: "mother-in-law"; "PhD advisor"; "committed atheist"; "goal: raise his children well" |
| **7** | identity hobbies, close friends, long-term collaborators, multi-year active goals: "plays chess daily, identifies as a chess player"; "close friends since high school" |
| **6** | regular routine engagement, not identity-defining: "weekly engineering syncs"; "drinks coffee each morning"; "sees a relative & Joe every couple months" |
| **5** | casual interests, light recurring states, soft goals: "occasionally plays Disco Elysium"; "likes Bob's Burgers"; "wants to read more this year" |
| **4** | one-time events that mattered briefly, weekly tools: "great Coachella 2024"; "uses VS Code as primary IDE"; "attended cousin's wedding" |
| **3** | routines, maintained lists, this-week goals: "Wednesday trash night"; "maintains a Ralph's shopping list"; "wants to clean the garage this weekend" |
| **2** | single events, list items, transient states, admired public figures: "headache today"; "watched a GOT episode"; "admires Bob Dylan" |
| **1** | structural metadata, passing brand mentions, momentary intents, contact info: "email is jukka@…"; "mentioned Bjork once" |
| **0** | extraction noise: data-pipeline labels, `is_a Concept` with no binding, contextless scaffolding |

Carve-outs the prompt enforces: contact-info targets (phone/email/URL/
address) → 1–3 regardless ("owning a phone number is not a relationship");
routine/task/device/list/document targets → 1–3 ("using a thing is not a
relationship"); family relationship types keep high scores even with an
empty target description (the type itself is the signal).

When the LLM omits an edge from its output, `scoring.DEFAULT_EDGE_SCORE = 5.0`
fills it — mid-scale, neither "important" nor "trivia."

Failure modes (documented in `regenerate_edge_importance`):
- **Source-perspective undervaluation** of contact-info edges ("a friend's
  phone number" isn't important *to a friend*). The entity-cards contact
  carve-out (a section-tag bypass of `CARD_FACT_FLOOR`) is the workaround.
- **Self-similar fan-out**: 15 "Purchase Habit --located_in--> Dairy Aisle"
  edges each score a reasonable 3–4, collectively inflating the hub's
  *derived* node importance. Addressed downstream by `importance/damping.py`,
  not by retouching edge scores.

## Entity / Concept importance — MAX over edges

`regenerate_entity_importance` sets

```
Entity.importance = MAX(edge.importance) over all incident edges
```

(both directions; NULL-importance edges excluded). SQL: a `GROUP BY n.id`
over `kg_node_metadata JOIN kg_edge_metadata` for `node_type IN ('Entity','Concept')`.

**Why MAX, not sum or average** (the calibration backbone, verbatim from
the docstring):
- *Sum rewards fan-out* — 100 mediocre edges would outrank 1 critical
  edge; Dairy Aisle (15 `located_in` edges) would beat a friend's mother
  (1 deep `parent_of`).
- *Average penalizes hubs* — the user has ~2200 edges, a few critical and
  many incidental; averaging dilutes the signal.
- *Max picks "the strongest reason anyone cares."* a friend is important
  because of his close-friendship bond (8.5), not the 50 incidental
  mention-edges.

**Why derived, not LLM-rated.** The removed LLM Entity rater scored the assistant
**3.0** despite 346 edges + 154 observations because the prompt
categorized her as an "AI tool" and the rater anchored on that. The edge
graph already encodes the truth — "the user cares deeply about the assistant" lives in
edges like `the user --addressee--> the assistant` at 9. Picking a number was the easy
part; the edges had done the hard part. Removing the LLM step removed the
misjudgments and a recurring cost.

Semantically the answer is "important to **someone** in the graph," not
"important to the user specifically." Where most edges are anchored on the user
or his circle the two coincide; for nodes deep in someone else's subgraph
they diverge. That residual "user-POV" layer is what `importance/damping.py`
is meant to supply (knocking down structural artifacts like Dairy Aisle
that clear the max-gate but fail any "matters to the user" check).

Failure modes: fan-out hub artifacts (above); sparse-Entity bias (a
one-edge Entity inherits that edge's optimism); NULL gating (excluded
edges make the derivation a lower bound that rises as the edge rater
backfills — which is why step 2 runs after step 1 every cycle).

## State / Event / Goal / Property importance — inherited via the edge chain

`regenerate_state_importance` sets

```
State.importance = MAX( other_end.importance × edge.importance / 10 )
                   over all edges touching the State
```

where `other_end` is the Entity on the far side of each connecting edge.
Both factors are 0–10; the `/10` keeps the product in 0–10. SQL: a
self-join from the State to its connecting edge to the far-end node, for
`node_type IN ('State','Event','Goal','Property')`, filtered on both
`edge.importance` and `src.importance` being non-NULL.

These node types are **connective tissue** — facts/events/aspirations
that Entities participate in. "a friend's phone number is X" is a State tying
a friend to a number node; its importance is borrowed from how much the
participating Entity cares about the link, which `edge.importance` already
captures.

**Why multiplication along the chain** (verbatim rationale):
- A high-importance Entity (a friend 8.5) tied via an unimportant edge (1)
  shouldn't pull the State up — the State derives importance from the
  *link* to a friend, not a friend's existence. The product zeroes out un-pulled
  connections.
- A low-importance Entity tied via a critical edge still registers
  (a roommate at 2.5 with a `has_state` at 7). Addition would over-credit
  the unimportant endpoint; the product captures "how much this PATH
  conveys."

**Why MAX across edges** — same fan-out concern as Entity importance:
sum would reward States touching many low-pull participants; max picks
the single strongest participant-edge combination.

**Why derived, not LLM-rated.** A State has no evaluative standing of its
own — its importance is fully a function of who participates and how much
they care. An LLM layer would re-derive the same signal less directly.

**Dependency order is mandatory.** `src.importance` is the Entity-side
value written in step 2; if it's NULL the join drops the edge and the
State gets no rating that pass. The routine sequences edge → entity →
state for exactly this reason. (`Property` nodes are subject-scoped, so
the subject's edge is the only/strongest tie and the derivation behaves;
a cross-subject edge from a bad merge would confuse it.)

Both node writers preserve `Node.updated_at` (self-reference in the
`UPDATE`) so a score-only change does **not** cascade into wiki / entity-card
refreshes — same protective pattern as `step_pagerank`. Both invalidate
the lens importance cache (`importance/cache.invalidate`) after writing,
or lens consumers serve stale values until restart.

## PageRank — weighted, structural, global (not personalized)

`pipelines/kg_maintenance_pipeline/step_pagerank.py` is the
`pagerank_score` producer (the doc's old "is PageRank done / is it
personalized?" TODO — resolved here). It runs **in the KG maintenance
pipeline**, not in the importance routine, and writes
`kg_node_metadata.pagerank_score`.

Properties:
- **Edge-weighted.** Weights are `edge.importance` (default `0.5` when
  NULL), so the edge rater's output is also PageRank's input — heavier
  edges propagate more influence between endpoints.
- **Passthrough-collapsed.** State/Event/Goal/Concept/Property nodes are
  treated as passthrough: every `Entity → Passthrough → Entity` path adds
  a virtual `Entity→Entity` edge (average of the two hop weights) so
  intermediate nodes don't absorb score. All nodes still receive a score;
  the primary signal is Entity-to-Entity connectivity.
- **Undirected, standard power iteration**: `DAMPING = 0.85`,
  `MAX_ITER = 100`, `TOLERANCE = 1e-6`; final scores normalized to the max.
- **Not source-biased.** There is no teleport bias toward the user — this is
  a global weighted PageRank, *not* a personalized one. (A personalized /
  source-biased PageRank remains the right tool if multi-path
  user-relative propagation is ever wanted; it is not what runs today.)
- Like the node writers, it preserves `updated_at` to avoid nightly
  refresh storms.

## The lens blend — how consumers combine the two signals

The personalized lens and the node-selection paths don't read raw
importance alone; they blend it with PageRank. The weights are the single
source of truth in `consumers.py`:

```python
LENS_BLEND_IMPORTANCE_WEIGHT = 0.4
LENS_BLEND_PAGERANK_WEIGHT   = 0.6
priority = 0.4 × node.importance + 0.6 × node.pagerank_score
```

`kg_core/kg_utils/node_importance.py` imports both constants and sorts
candidates by this blend in `get_important_nodes` (structural gate →
blended priority sort → optional nano-LLM junk filter), used by the
description-fill and entity-card pipelines. NULL importance/pagerank fall
back to `DEFAULT_SCORE = 0.5` in the snapshot read.

**Why PageRank-favored (0.6).** PageRank is structural (connectedness ×
edge weights) and stable; node importance is per-node and more current
but was historically NULL for freshly-promoted nodes — leaning on PR keeps
a sensible ordering even when importance is incomplete. Now that the
nightly derivation backfills importance, the weights could shift toward
importance, but that is a calibrated decision, not a casual edit (keep the
sum at 1.0 to stay in the 0–10 range).

## Importance from the source's perspective — the design rationale

The edge rater's "from the source's perspective" framing is the reason
the system needs only one persisted importance column per node rather than
one per (node × lens). Each edge already encodes a node's importance under
its source's lens; composing along edges recovers any other lens on
demand.

The **multiplicative chain** is the general form of that idea, and it is
exactly what `regenerate_state_importance` implements for the one-hop case
(Entity → edge → State): `other_end.importance × edge.importance / 10`.
The summer-house intuition generalizes it:

```
imp(summer_house, from user)  ≈  imp(Alice, user) × imp(summer_house, Alice) / 10
```

A place the user has never been rates ~2 directly, ~8 under Alice's lens,
and ~7 "in the context of Alice" via the chain — all three correct for
different uses (global ranking → direct; conversation-about-Alice ranking
→ chain through Alice; Alice's wiki page → Alice's own edges). The `/10`
keeps the result on the 0–10 scale.

Where the chain stops being the right tool:
- **Sign / role changes** — "Alice hates Bob" carries high salience but
  flips sign; the edge rater stores raw importance, not valence, so chain
  results read as "high gravity in your network via this path," not "you'd
  like this node."
- **Multiple paths** — a single product ignores the others; a relative
  reachable both directly and through a spouse should accumulate both.
  This is precisely what PageRank's multi-path propagation handles and the
  chain does not — which is the division of labor: the deterministic
  derivations do single-hop inheritance, PageRank does multi-path
  structural flow, and the lens blends the two.
- **Long chains** — edge-weight noise compounds multiplicatively; past
  two or three hops the product says more about chain length than about
  the relationship.

## Consumers — who reads importance and for what

Edge importance (`edge.importance`):
- PageRank edge weights (`step_pagerank.py`).
- Entity/State node derivation (steps 2–3 above).
- Card fact admission fallback when a node's importance is NULL
  (`entity_cards_v2/builder.py::_collect_candidate_facts`).
- KG search ranking, convergence-graph activation, graph visualizer
  thickness/opacity.

Node importance (`node.importance`):
- **Card-worthiness** — `consumers.is_card_worthy`: pass on user-lock, or
  `observation_count ≥ 2 AND degree ≥ CARD_MIN_DEGREE (15)`, or
  `observation_count == 1 AND degree ≥ 10`, or
  `effective_importance ≥ CARD_HIGH_IMPORTANCE_FLOOR (7.0)`. The
  observation gate alone was too permissive (common nouns "AC", "Lights",
  "Yogurt" rack up observation counts in ordinary English); the degree
  conjunction filters that bloat.
- **Card fact floor** — `CARD_FACT_FLOOR = 0.6` admits a connected fact to
  a card; `CARD_FACT_MIN_KEEP = 50` is the sparse-entity safety net.
- **Wiki growth** — `consumers.is_wiki_growth_candidate`:
  `degree ≥ WIKI_GROWTH_MIN_DEGREE (4) AND effective_importance ≥ WIKI_GROWTH_IMPORTANCE_FLOOR (5.0)`.
- **Wiki refresh gating** — `WIKI_REFRESH_CHANGE_FLOOR = 4.0`
  (`wiki_generator/nightly_refresh.py`).
- **Context-engine activation** — `CONTEXT_ACTIVATION_THRESHOLD = 0.5`,
  `CONTEXT_ACTIVATION_SECOND_WAVE_THRESHOLD = 0.4` (Entity/Concept admitted
  by type regardless).
- **Timeline** — confirmation-prompt floor `TIMELINE_CONFIRMATION_FLOOR = 6.0`;
  `display.importance_marker` ★ / · band.
- **State decay** priority and noteworthy-closure floor
  (`STATE_DECAY_NOTEWORTHY_FLOOR = 5.0`).
- **Date-gap scan** — `DATE_GAP_WORTH_FLOOR = 4.0` with high/medium bins
  (`date_gap_priority`).
- **Lens layout** — the 0.4/0.6 blend above.
- **KG investigator finding priority**, **section-tagger admission**, and
  the **graph visualizer** visibility slider.

`effective_importance` (`importance/effective.py`) is the damping-adjusted
read used by the gates above, layered on top of the raw derived value.

> Note (out of scope for this doc): `kg/proposal_promoter.py` still carries
> comments (~lines 1192, 1243) describing `me::importance_rater` as the
> Entity rater. Those are stale — Entity importance is derived now. The
> code path is correct (promotion leaves `importance` NULL for the routine
> to fill); only the comments lag.
