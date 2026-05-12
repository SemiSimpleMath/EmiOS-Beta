# Node and edge importance

How the KG decides which nodes/edges matter to the user, who scores them,
when, and where the gaps are.

## What gets rated

| Subject | Rater | When | Scale |
|---|---|---|---|
| **Entity** nodes | `me::importance_rater` agent | `kg_importance_rater` routine (interval, every ~30 min, only_unrated) | 0–10 (per agent prompt), persisted to `kg_node_metadata.importance` |
| **kg_edge** rows | `me::edge_importance_rater` agent | same routine | 0–10 from source's perspective, persisted to `kg_edge_metadata.importance` |
| **State / Event / Goal / Property** nodes | **(no rater)** | — | column exists; populated only by ad-hoc paths (proposal promotion, manual writes) |

Code paths:
- Routine: `app/assistant/routine_manager/routine_functions.py` `_lazy_kg_importance_rater`
- Entity rater: `app/me/importance.py` `regenerate_importance()` — defaults `only_node_types=["Entity"]`
- Edge rater: `app/assistant/kg/edge_importance.py` `regenerate_edge_importance()` (relocated from `app/me/` 2026-05-11 — general KG infrastructure, not lens-only)
- Entity rater agent: `app/assistant/agents/me/importance_rater/`
- Edge rater agent: `app/assistant/agents/me/edge_importance_rater/`

Both rater agents live under `me::` because the lens being scored is the
relationship-from-the-user's-perspective lens.

## How Entity importance is determined

`me::importance_rater` (`app/assistant/agents/me/importance_rater/`) is
an LLM agent. The driver `regenerate_importance()` pulls Entity nodes
in batches of 60, fetches each node's edges (capped per node), renders
a numbered block per node (label + node_type + category + description
+ neighbor edges), and asks the agent to return one 0–10 score per id.

The prompt anchors calibration explicitly:

| Band | Examples |
|---|---|
| 9.0–10.0 | Self, partner, children, parents, very close family. Critical home/job. Defining lifelong friends. Formative mentors who shaped the user's life path |
| 7.0–8.9 | Siblings, in-laws, very close friends, primary employer, central long-term project, significant teachers/collaborators |
| 5.0–6.9 | Cousins, regular colleagues, ongoing pets, regular places (home city, frequent restaurants) |
| 3.0–4.9 | Old colleagues, neighbors, occasional friends, hobbies, recurring services |
| 1.0–2.9 | One-off mentions, products / brands / video games / movies / TV shows mentioned in passing |
| 0.0–0.9 | Generic concepts, taxonomy scaffolding, email subject lines, junk extracted from automated messages |

Plus a **lens-specific bias**: this is a relationship lens. People,
organizations, places, and groups can score 7+. Phone numbers, email
addresses, dates, prices, calendar events, devices, files, documents
score 0–3 regardless of who they belong to or how often they appear.

Past-tense ("the late X") and closed-era relationships (deceased
mentor, ex-employer that defined a career) are explicitly NOT lowered
— they're still defining presences.

Persisted to `kg_node_metadata.importance`. The routine runs in
`only_unrated=True` mode by default, so the rater backfills new
content rather than re-rating settled scores every cycle.

## How Edge importance is determined

`me::edge_importance_rater` (`app/assistant/agents/me/edge_importance_rater/`)
rates edges **from the source node's perspective** on the same 0–10
scale. The driver pulls edges in batches and renders each as
(source label, relationship_type, target label, edge sentence).

Two key disciplines from the prompt:

1. **Reason before score.** The agent must write a 1–2 sentence reason
   restating what the edge sentence describes, why it matters to the
   source, and which calibration band fits. Forcing the reason first
   keeps the score honest.
2. **Read the edge sentence, not the relationship_type.** `has_state`
   covers everything from "is deceased" (9–10) to "has email address"
   (1–2). The label is a coarse extraction bucket; the sentence is
   what's being scored.

Calibration:

| Band | Examples |
|---|---|
| 9.0–10.0 | `married_to`, `parent_of`, `child_of`, `sibling_of` to immediate family. Primary employer / lifelong career anchor |
| 7.0–8.9 | `close_friend_of`, `mentor_of`, formative teacher, central long-term collaborator, in-laws |
| 5.0–6.9 | `colleague_of`, `friend_of` (regular), `neighbor_of` (engaged), recurring teammates, ongoing pets |
| 3.0–4.9 | `met_at`, `knows`, peripheral coworkers, one-time collaborators |
| 1.0–2.9 | `attended`, `participated_in`, public figures admired but never met |
| 0.0–0.9 | Extraction artifacts, generic concept relations, taxonomy scaffolding, contextless edges |

The "from source's perspective" framing means asymmetry is preserved:
a father may be defining to his son (10); the same son may be one of
many to the father (still typically 9–10 for immediate family, but
the asymmetry shows up at the edges of the band).

Persisted to `kg_edge_metadata.importance`.

## How importance of other node types is determined

**It currently isn't, in any systematic way.** State / Event / Goal /
Property nodes have an `importance` column that exists and is read by
many consumers, but no rater fills it. What's there is whatever a
proposal promoter or manual write happened to set, often the
documented default `0.5` or `NULL`.

The architectural intent is that these node types inherit importance
from the edges that touch them — a State linking Jukka and Katy by an
`is_married` edge weighted 10.0 should be ~10/10; a State linking a
peripheral entity by a generic `topic` edge should be near zero. The
edge rater already produces the input to this derivation (per-edge
importance from the source's perspective). What's missing is the
aggregation step.

Until a rater is wired in, the runtime workaround in
`app/assistant/kg_investigator/finding_priority.py` reads
`state.importance` when set, otherwise computes
`MAX(edge.importance) / 10` over edges touching the node — the same
signal a State rater would aggregate. This is per-call, not persisted.

## Compositional techniques in use today

Two patterns are implemented in production code:

### Direct read (`node.importance` / `edge.importance`)
Cheapest pattern. Reads the rater's stored score. Falls back to
`DEFAULT_SCORE = 0.5` when the value is NULL — the source of most
"why does Marika & Joe's marriage rank below Seija's hiking?"
surprises, since a never-rated marriage and a rated-but-modest
hiking preference both compare against 0.5.

Used everywhere: every consumer of `kg_node_metadata.importance`.

### Blended importance × PageRank
`app/assistant/kg_core/kg_utils/node_importance.py:27-28`:
```
priority = 0.4 × node.importance + 0.6 × node.pagerank_score
```

PageRank dominates because it's structural (connectedness × edge
weights), while raw importance is one rater's per-node call.
PageRank is computed offline and stored on `kg_node_metadata.pagerank_score`;
edge weights (the edge rater's output) determine how importance flows
along each edge during the PageRank computation. Used for ranking
candidates in the wiki-page generator and the entity-card pipeline.

### Max-of-edges fallback (for unrated nodes)
The drain ordering in
`app/assistant/kg_investigator/finding_priority.py` reads
`node.importance` when set, otherwise computes
`MAX(edge.importance) / 10` over edges touching the node — same
signal the State rater would aggregate, computed inline so unrated
nodes don't get buried until a rater catches up.

This is a per-call workaround, not a persisted pattern. The
intended-but-unbuilt State rater would persist this aggregation.

## Compositional theory (planned)

The patterns above are descriptive — they capture what's wired in
today. Two further compositions are theoretically useful and would
become relevant once a State rater exists, but **are not implemented
anywhere**:

### Multiplicative chain (transitivity along edges) — PLANNED

Importance flows along edges. If the user's friend Alice scores 8 from
the user's perspective, and Alice's father Bob scores 9 from Alice's
perspective, then Bob's importance to the user via this chain is:

```
imp(Bob from user)  ≈  imp(Alice from user) × imp(Bob from Alice) / 10
                    =  8 × 9 / 10  =  7.2
```

The `/ 10` keeps the result on the same 0–10 scale (both factors are
0–10).

**This is the lens-aware importance mechanism.** The architecture has
one persisted "lens" for entities — the user's lens, computed by
`me::importance_rater`. But the edge rater scores **every edge from
the source node's perspective**, not the user's. That means the
chain composition gives you importance under any other lens for free,
without storing per-(node × lens) importance columns.

Canonical example — the friend's summer house:

| Lens | Score | Interpretation |
|---|---|---|
| User direct rating (entity) | ~2 | A place the user has never been; low engagement |
| Friend Alice's lens (via edges) | ~8 | Alice's vacation home — central to her summers |
| User's lens via chain through Alice | `imp(Alice, user) × imp(summer_house, Alice) / 10` ≈ `9 × 8 / 10 = 7.2` | "Important to the user **in the context of Alice**" |

All three numbers are correct simultaneously for different uses:
- Ranking what the user cares about globally → use the direct rating
- Ranking what to surface during a conversation about Alice → use the
  chain through Alice
- Generating Alice's wiki page → use Alice's lens (the edges
  originating from Alice)

This is why we don't need per-(node × lens) importance columns. The
edge rater's "from source's perspective" framing already encodes every
node's importance under every other node's lens; chain composition
recovers it on demand.

Where the chain breaks down:
- **Sign / role changes along the chain.** A friend's hated coworker
  is not `imp(friend) × imp(coworker, friend) / 10` because the
  edge "Alice hates Bob" has high *salience* but the relationship
  changes sign. The current edge rater doesn't carry sign — it rates
  raw importance to the source. Chain results have to be read as
  "this node has high gravity in your network via this path," not
  "you would like this node."
- **Multiple paths.** The product of one chain ignores the others. A
  family member with strong direct AND through-spouse paths to the
  user should accumulate both. PageRank handles this correctly; the
  chain doesn't.
- **Long chains.** Edge-weight noise compounds multiplicatively.
  Past two or three hops, the chain produces nearly-uniform scores
  that say more about chain length than relationships.

Use cases: cheap "how important is X to me?" queries where X is one
or two hops away and the relationship sign is unambiguous (family of
friends, employer of mentor). For longer or branchier paths, fall
back to personalized PageRank.

### Personalized PageRank (multi-path propagation) — POTENTIALLY ALREADY DONE

Full-graph propagation, source-biased on the user. The right tool
when multiple paths between the user and a target should sum, not
take the max of one path. Edge weights (the edge rater's output)
determine flow rate along each edge.

`kg_node_metadata.pagerank_score` is populated, but I haven't
verified whether the current pagerank computation is actually
*personalized* on the user (vs a global pagerank) or whether it
weights edges by the edge rater's importance scores. Worth verifying
before relying on it as the multi-path tool. (TODO: read the
pagerank_score producer.)

### State / Event / Goal rater — PLANNED (the main missing piece)

Two viable shapes:

1. **Deterministic SQL aggregate.** For each unrated State, set
   `importance = MAX(edge.importance) / 10` over its edges, gated on
   the edge rater having run. Cheap; aligned with the "state
   importance comes from edges" intent. Risk: max collapses when many
   moderate edges exist (a State touching ten 4.0 edges scores the
   same as one touching a single 4.0 edge). A weighted sum or
   importance-aware aggregate would handle that better but is more
   complex.
2. **Parallel LLM rater.** A prompt similar to `me::importance_rater`
   but tuned for States (different calibration: life events vs
   preferences vs background facts). Pass
   `only_node_types=["State", "Event", "Goal"]` to
   `regenerate_importance`. More flexible; spends LLM tokens on
   something the edge data already encodes most of.

Recommendation: ship #1 first (closes the 41% NULL gap deterministic-
ally and cheaply). Add #2 later if the SQL aggregate produces
inversions (e.g., a defining-life-event State outranked by a
trivially-mentioned-many-times State because of edge count).

### Picking the right composition

| You want… | Use | Status |
|---|---|---|
| Rank a single node fast | direct read with edge-fallback for nulls | implemented (drain queue) |
| Rank candidates for wiki / card generation | blended importance + pagerank | implemented |
| Rank a State that the rater missed | `MAX(edge.importance) / 10` | implemented (drain queue, per-call) |
| Persist State importance | State rater (deterministic or LLM) | **planned** |
| "How important is X to me?" — short chain | multiplicative chain | **planned** |
| "How important is X to me?" — multi-path / hub effects | personalized PageRank | partially built (pagerank_score column populated; verify producer) |

## The gap: State / Event / Goal nodes

State (and Event, Goal, Property) importance values exist but **no agent
periodically re-rates them.** What's there came from inconsistent paths
at proposal promotion or earlier code that's no longer wired in.

Distribution snapshot (May 2026, 3,352 State nodes):

| State.importance | Count | % |
|---|---|---|
| NULL | 1,381 | 41% |
| exactly 0.5 (the documented `DEFAULT_SCORE`) | 98 | 3% |
| < 0.5 | 356 | 11% |
| > 0.5 | 1,517 | 45% |

`DEFAULT_SCORE = 0.5` lives at `app/assistant/kg_core/kg_utils/node_importance.py:29`
and gets used by every consumer that does `or DEFAULT_SCORE` when reading
the column. So a State at exactly 0.5 in the DB is effectively
indistinguishable from a State the rater never visited.

## The intended-but-unbuilt derivation

State importance is conceptually "how important is this State to the
entities it connects?" — i.e., a function of the importances of the
edges that touch it (which the edge rater scores from the source's
perspective). A State linked to Jukka and Katy by `is_married` edges
weighted 10.0 should rank ~10/10; a State linked to a peripheral entity
by a generic `topic` edge weighted 0.8 should rank low.

**There is no code that performs this derivation.** Nothing reads
`kg_edge_metadata.importance` to compute or backfill
`kg_node_metadata.importance` for a State node.

Verified empirically:

| State | state.importance | max edge.importance touching it |
|---|---|---|
| "Married to Katy" (Jukka–Katy) | 0.95 (rated somehow) | 10.0 |
| "Marriage" (Marika & Joe) | **0.50** (looks like default fallback) | — |
| "Residence — Irvine move" (`a4628fc3`) | NULL | 7.0 |

The Marika+Joe case is almost certainly an unrated State sitting at the
default — the user's surfaced concern that triggered this audit.

## Consumer behavior given the gap

The `kg_finding_priority` drain ordering (`app/assistant/kg_investigator/finding_priority.py`):
- Reads `state.importance` directly when set
- Falls back to `MAX(edge.importance) / 10` across edges touching the
  node when `state.importance IS NULL` — same signal the rater would
  use, computed inline so unrated nodes don't get buried

The wiki nightly refresh (`app/assistant/wiki_generator/nightly_refresh.py`):
- Defers re-rendering a page until `kg_importance_rater` has rated the
  node — but since States never get rated, this affects only Entities
  in practice.

The lens (`app/me/`) and entity-card pipeline both apply
`importance × 0.4 + pagerank × 0.6` blends with `or DEFAULT_SCORE` (0.5)
fallbacks. Half the State nodes contributing through this blend are
contributing the default rather than a real score.

## What would close the gap

A State/Event importance rater that runs in the existing
`kg_importance_rater` routine alongside the entity + edge raters. Two
viable shapes:

1. **Derived (deterministic).** A SQL aggregate: for each unrated
   State, set `importance = MAX(edge.importance) / 10` over its edges,
   gated on the edge rater having actually run. Cheap; assumes the edge
   rater's scoring is the right signal (it generally is — it's already
   the agreed-upon "from source's perspective" measure).
2. **LLM-rated (parallel to Entity).** A prompt similar to
   `me::importance_rater` but tuned for States (different calibration:
   life events vs preferences vs background facts). Pass
   `only_node_types=["State", "Event", "Goal"]` to `regenerate_importance`.

Option 1 is cheaper, deterministic, and aligned with the architectural
intent ("state importance comes from edge importance"). Option 2 is
more flexible but spends LLM tokens on something the edge data already
encodes.

Either way, the routine wiring change is small: extend
`_lazy_kg_importance_rater` to also rate States after edges are scored.
