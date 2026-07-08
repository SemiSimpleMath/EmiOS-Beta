# Meal Planning Subsystem (end-to-end reference)

> Audience: an engineer or agent who needs to understand, use, or modify EmiOS's
> autonomous meal-planning and grocery subsystem. This is the canonical "how it
> actually works" doc. Belief mechanics live in `16_BELIEF_ENGINE.md`; pod storage
> in `14_PODS.md`; scheduling in `06_PIPELINES_AND_ROUTINES.md`. Last substantial
> update: 2026-06-16.

---

## 0. TL;DR

A **two-tier autonomous planner**, decoupled through the pod store (no direct calls
between tiers):

- **Weekly planner** (cron, Sunday) lays out a 7-day × 3-window slot grid + a
  categorized shopping list → one `plan.weekly_meals` pod + a Google Doc shopping list.
- **Daily proposer** (cron, early morning) turns the next 24–48h of slots into concrete
  dishes, adapting to inventory / diet log / schedule → `intention.meal` +
  `intention.shopping` + `intention.meal_set` pods (advisory; the scheduler arbiter
  decides what reaches the calendar).

Personalization comes from the **knowledge graph** (per-person dietary constraints),
the **belief store** (learned food preferences), and a **closed feedback loop**: the
user comments on the `/meals` page → those comments become beliefs → the next plan
shifts. The whole thing extends existing primitives (pods, KG, beliefs, routines) — no
bespoke datastore.

---

## 1. The two tiers

Both tiers share one leaf agent design but run through different orchestration:

| Tier | Cadence | Model tier | Output |
|---|---|---|---|
| Weekly planner | Sunday (targets the **upcoming** Monday) | smart | `plan.weekly_meals` pod + weekly Google Doc shopping list |
| Daily proposer | early morning | mini | `intention.meal` / `intention.shopping` / `intention.meal_set` pods |

The tiers never call each other. The weekly planner writes a `plan.weekly_meals` pod
(`for_agents=[daily_meal_proposer]`); the daily proposer reads the latest such pod as
its skeleton and fills slots with concrete dishes. This pod-mediated decoupling means
either tier can run, fail, or be re-run independently.

### Weekly planning chain (unified, one path)

Three entry points — the Sunday cron, the `/meals` "Generate" button, and the CLI — all
run the **same** function, `weekly_meal_planning_runner.run_weekly_meal_planning_chain`:

```
build seed (meal_context_builder)
  → meal_context_distiller            # reorganizes seed into per-day meal_context
  → weekly_meal_planning_manager      # delegator → planner → [meal_research] → weekly_meal_planner
  → meal_persist.apply_weekly_meal_planner_output   # mint pod + write Google Doc
```

The distiller is load-bearing: the `weekly_meal_planner` agent's prompt renders its
entire personalization block under `{% if meal_context %}`, and only the distiller
produces `meal_context`. (Historically the cron called the leaf agent directly and
skipped the distiller, so autonomous plans were made blind to dietary constraints,
audience, and concerns — fixed by unifying on the chain.)

### Daily proposing

`run_daily_meal_proposer` → `build_daily_meal_proposer_context` (includes the latest
`plan.weekly_meals` pod, inventory, diet log, calendar, beliefs, the easy-meals rotation,
and the arbiter's `weekly_schedule` block) → `daily_meal_proposer` agent →
`meal_persist.apply_daily_meal_proposer_output` mints the intention pods.

### Easy-meals rotation

`easy_meals.py` is a small structured "go-to dishes" layer (`resource_easy_meals.json`)
that sits **alongside** the hand-edited markdown food registry — each entry carries a
`max_days` cadence ("OK with this at most every N days") + free-text notes. "Days since
last" is **not stored**: `build_easy_meals_rotation()` derives it from recent
`plan.weekly_meals` + `intention.meal` pod history (token-match dish names, only *past*
dates count), tagging each dish `DUE` / `RESTING` / `never`. `render_easy_meals_for_planner`
feeds this into both context builders under the **`easy_meals_rotation`** key so the
proposer prefers DUE dishes and avoids RESTING ones.

### Scheduler-arbiter layer

The proposers are advisory; a separate **scheduler arbiter** decides what actually reaches
the calendar. `build_scheduler_arbiter_context` aggregates every fresh `intention.*` pod
across proposers (`intention.meal` / `intention.wellness` / `intention.romantic`) over the
next 14 days plus the household calendar (hard constraint), and the
`scheduler_arbiter` agent → `scheduler_arbiter_persist` mints **one** `plan.weekly_schedule`
pod with `is_anchor` flags (hard conflicts surface as dayflow tickets). The daily proposer
reads that pod back via `build_weekly_schedule_block` (the `weekly_schedule` context key)
and treats `is_anchor` items as locked. So the meal loop is:
`daily_meal_proposer → intention.meal pods → scheduler_arbiter → plan.weekly_schedule →
(next) daily_meal_proposer`.

---

## 2. Component map

| Component | File | Role |
|---|---|---|
| Routine handlers | `routine_handlers/subconscious.py` | wrap runners as scheduled routines |
| Context builder | `subconscious/meal_context_builder.py` | assemble context for daily/weekly/distiller (incl. the belief lane + easy-meals rotation) |
| Easy-meals registry | `subconscious/easy_meals.py` | structured "go-to dishes" list + DUE/RESTING rotation derived from planned history |
| Scheduler arbiter | `subconscious/scheduler_arbiter_context_builder.py`, `scheduler_arbiter_persist.py` | synthesize `intention.*` pods into one `plan.weekly_schedule` |
| Weekly chain | `subconscious/weekly_meal_planning_runner.py` | the single canonical weekly path |
| Persist | `subconscious/meal_persist.py` | mint pods + write Google Doc shopping list |
| Inventory | `subconscious/grocery_inventory.py` | load/mutate/save inventory; acquire/consume/decay |
| Grocery sync | `subconscious/grocery_sync_runner.py` | scan recent chat for inventory intents + apply decay |
| Shopping consolidation | `subconscious/meal_email_renderer.py` | `consolidate_intention_shopping` (dedup ad-hoc items for the `/meals` view-model; the old "Send to the user's partner" email rendering was removed) |
| Feedback | `subconscious/feedback_extractor_*` | turn `/meals` comments into beliefs |
| Page service | `subconscious/meal_page_service.py` | `/meals` view-model + Generate button |
| Agents | `agents/{daily_meal_proposer, weekly_meal_planner, weekly_meal_planning, meal_research, meal_context_distiller, grocery_intent_scanner, feedback_extractor}` | LLM decisions |
| Managers | `multi_agents/{meal_research_manager, weekly_meal_planning_manager}` | research detour + planning chain |
| Surface | `routes/meals.py`, `templates/meals.html` | weekly grid + shopping list + comment boxes |

### Data stores

- **Pods** (two-axis: kind + variant tag): `plan` #weekly_meals (weekly grid),
  `intention` #meal (per proposal), `intention` #shopping (ad-hoc run),
  `intention` #meal #run_summary (per-run summary),
  `plan` #weekly_schedule (arbiter output, shared by all proposers),
  `feedback` #comment (a user comment awaiting extraction). Consumers query
  `kind="intention", tags=["meal"]`; exactly one variant tag per family pod
  is enforced at mint.
- **Resources** (gitignored, food PII): `resource_easy_meals.json` (easy-meals registry,
  seeded once from the markdown food registry's "Comfort food subset").
- **Resources** (`resources/subconscious/`, gitignored where they hold personal data):
  grocery inventory, the read-only external standing list, the agent's weekly Doc state,
  grocery-sync scanned-id state.
- **SQLite**: `user_beliefs` + `belief_evidence` (food beliefs), KG `Node`/`Edge`
  (per-member dietary state), `unified_log_2026` (grocery sync reads `role=user` rows).
- **External**: Google Docs (a read-only standing list; the agent's weekly shopping list,
  created then diff-replaced).

---

## 3. The feedback loop (comment → belief → plan)

This is what makes the planner improve over time. The full path:

```
user comments on a plan/meal pod via /meals
  → feedback.comment pod (route app/routes/subconscious.py)
  → feedback_extractor agent (routine feedback_extractor_run, daily; CLI run_feedback_extractor)
  → feedback_extractor_persist.apply_feedback_extractor_output
       → BeliefStore.upsert_belief  (belief_engine, table user_beliefs + belief_evidence)
  → meal_context_builder._build_food_beliefs()  (tag-scoped retrieval lane, §3.1)
  → distiller → planner            # next plan reflects the feedback
```

Three properties that are easy to get wrong and are pinned by guard tests:

### 3.1 How the meal lane retrieves beliefs (`_build_food_beliefs`)

`_build_food_beliefs` (`meal_context_builder.py`) branches on the subsystem flag
`meal_beliefs_v2` (default **ON**, toggled in `/dev/subsystems`). A lane failure is
**loud** — ERROR log + an explicit `(BELIEF LANE ERROR …)` marker in the prompt — never
a silent swap to the other lane (the flag is the human's switch).

**ON → `_build_food_beliefs_v2` (the live lane).** A single relevance + recency +
frequency-ranked, **tag-scoped** candidate set, via
`belief_engine.retrieval.beliefs_for_context(query=_MEAL_BELIEFS_QUERY,
tags=pull_set("meal_engine"), k=40)`. The retrieval scorer (`belief_engine/retrieval.py`)
ranks active `user_beliefs` by `0.55·relevance` (embedding cosine to the query) +
`0.25·recency` (30-day half-life on `last_confirmed`) + `0.20·frequency` (log-saturating
`observation_count`). Beliefs whose `last_confirmed` is within 21 days are stamped
`recent` in the rendered block and instructed to override older preferences; episodic
ones ("stomach bug") carry a time-scope-with-common-sense instruction off their observed
date. **Naming trap:** the function is called `_v2` for continuity with the
`meal_beliefs_v2` *flag*, but it reads the **v1** belief store (`emi.db`: `user_beliefs` +
`belief_tags` + `belief_short_id`) — belief-engine v2 was retired as the primary producer
(2026-06-16), so `_v2` here means "the new retrieval lane", not "the v2 store".

**OFF → `_build_food_beliefs_v1` (legacy, flag off).** The old two-lane design over
`BeliefStore`: (1) an **established lane** of food-domain beliefs (matched by
`belief_key` prefix) ranked `current_net_weight DESC`, top-30; (2) a **recency lane** of
food beliefs with a `user_comment` evidence row in the last 21 days, surfaced **ahead**
of the established lane and deduped. The recency lane existed because weight-ranking
buries fresh feedback (a brand-new belief has `current_net_weight = NULL` until the
nightly recompute, and even once weighted sits below the cutoff); it keyed on **evidence
recency** (not `last_confirmed`, which the nightly pipeline bumps broadly) and
**excluded contradicts-only beliefs** so a just-refuted preference didn't reappear as
current intent.

### 3.1.1 The tag layer — how the lane reaches the right beliefs

The v2 lane pulls by a **tag set**, not a single domain. `pull_set("meal_engine")`
(`belief_engine/tagging.py`, reading `configs/belief_tags.yaml`) resolves to
`[food, meal, cooking, dining_out, beverage, snack, groceries, dietary]`. A belief
surfaces if it carries **any** tag in that set. `dietary` is the cross-domain **bridge**
tag — the only path by which a belief filed under the `health` domain ("GERD avoid
spicy") reaches the meal planner. Tags are an **additive retrieval layer**: a separate
`belief_tags` table, written by the belief categorizer and validated against the
controlled vocabulary in `belief_tags.yaml`. Retrieval scopes to the tagged slice **only
when the store is actually tagged** — on an untagged store it stays high-recall (returns
all, never nothing).

### 3.2 The `meal` belief domain exists only for recompute coverage

`belief_key` `domain` (`configs/belief_domains.yaml`) is a **separate axis** from tags:
it is the nightly *derivation* lane, not the retrieval scope. The `meal` domain (id
`meal`, `enabled: true`, `decay_enabled: false`) carries empty `tags`/`ticket_types` **on
purpose**. It exists so the nightly `belief_engine` pipeline's `RecomputeBeliefSnapshotStep`
+ `ReevaluateBeliefsStep` cover beliefs the `feedback_extractor` writes under
`domain='meal'` — without a registered domain those rows are orphaned (never get a
snapshot weight, never have contradictions reconciled). Empty `tags` means the pipeline's
evidence-collection/`UpdateBeliefsStep` finds nothing and cleanly skips the LLM derivation
lane (meal beliefs come from user feedback, not insight mining); only recompute/reevaluate
run on the existing rows.

### 3.3 Contradictions weaken, never fabricate

When a user expresses a dislike, the extractor may emit both a `confirms` on the dislike
belief and a `contradicts` on the opposite ("they *do* like it") belief. A `contradicts`
extraction must only **weaken an existing** belief
(`BeliefStore.add_evidence_to_existing`); it must **never create** one. Creating an
affirmative belief from a contradiction manufactures a "phantom" belief that carries only
negative evidence and that nobody ever asserted.

---

## 4. Reliability notes (for anyone touching persist / context)

The subsystem follows the repo's **fail-loud** rule strictly here, because a past habit of
`except Exception → log warning → return None` turned load-bearing failures into silent
no-ops (an entire tier persisted nothing for weeks without erroring). When editing the
mint helpers or context builders:

- Mints (`meal_persist._mint_*`) log and **re-raise** on error — never return `None`.
- The grocery sync state loader fails loud on a corrupt state file (silently resetting it
  would re-apply every prior chat message and double-count inventory).

### Guard tests (on the pre-push regression suite)

- `test_meal_pod_mint_guard.py` — every pod kind actually mints (catches undefined-name
  regressions in the mint helpers).
- `test_meal_beliefs_v2_lane.py` — the live lane: `_build_food_beliefs_v2` renders
  context-ranked beliefs from the v1 store and marks recently-confirmed ones as current
  intent; the dispatcher honors the `meal_beliefs_v2` flag (off → legacy lane); a lane
  failure with the flag ON is loud (explicit marker, no silent swap).
- `test_meal_food_beliefs_recency.py` — the **legacy** lane (`_build_food_beliefs_v1`): a
  fresh, low/NULL-weight belief reaches the planner via the recency lane even when
  outranked; a contradicted-only belief does not.
- `test_feedback_extractor_no_phantom.py` — a `contradicts` against a non-existent belief
  mints nothing; against an existing belief it weakens, not creates.

---

## 5. Surfaces & cadence

- `/meals` (`routes/meals.py`, `templates/meals.html`): weekly grid, shopping list (the
  live Google Doc body + ad-hoc `intention.shopping` items), and per-day/week/shopping
  comment boxes that feed the learning loop.
- Routines (`configs/routines/public/`), in daily order:
  - `subconscious_grocery_sync` — 04:30–05:00 (so inventory is fresh for the proposer).
  - `subconscious_daily_meal_proposer` — 05:00–05:30.
  - `subconscious_scheduler_arbiter` — 05:30–06:00 (runs *after* the proposers).
  - `subconscious_meal_feedback` — hourly, 07:00–21:00 (ask "how was <dish>?" + ingest the reply).
  - `subconscious_weekly_meal_planner` — Sunday 17:00 (targets the upcoming Monday).

  The nightly `belief_engine` pipeline (`RecomputeBeliefSnapshotStep` + `ReevaluateBeliefsStep`)
  recomputes the belief snapshot and reconciles contradictions over registered domains,
  including `meal`.

---

## 6. Genuinely well-built (fair credit)

Pod-mediated weekly→daily decoupling; strict, well-documented Pydantic schemas with a
coercion net; sensible model tiering; the closed feedback loop (correctly timed so
overnight feedback lands in the morning plan); two-tier KG/belief food-keyword filtering
with runaway-density surfacing; idempotency design (inventory-applied markers, scanned
chat-id state); the distiller pattern; solid `/meals` week navigation.
