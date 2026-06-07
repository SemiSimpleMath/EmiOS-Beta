# Meal Planning Subsystem (end-to-end reference)

> Audience: an engineer or agent who needs to understand, use, or modify EmiOS's
> autonomous meal-planning and grocery subsystem. This is the canonical "how it
> actually works" doc. Belief mechanics live in `16_BELIEF_ENGINE.md`; pod storage
> in `14_PODS.md`; scheduling in `06_PIPELINES_AND_ROUTINES.md`. Last substantial
> update: 2026-06-07.

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
`plan.weekly_meals` pod, inventory, diet log, schedule, beliefs) → `daily_meal_proposer`
agent → `meal_persist.apply_daily_meal_proposer_output` mints the intention pods.

---

## 2. Component map

| Component | File | Role |
|---|---|---|
| Routine handlers | `routine_handlers/subconscious.py` | wrap runners as scheduled routines |
| Context builder | `subconscious/meal_context_builder.py` | assemble context for daily/weekly/distiller |
| Weekly chain | `subconscious/weekly_meal_planning_runner.py` | the single canonical weekly path |
| Persist | `subconscious/meal_persist.py` | mint pods + write Google Doc shopping list |
| Inventory | `subconscious/grocery_inventory.py` | load/mutate/save inventory; acquire/consume/decay |
| Grocery sync | `subconscious/grocery_sync_runner.py` | scan recent chat for inventory intents + apply decay |
| Feedback | `subconscious/feedback_extractor_*` | turn `/meals` comments into beliefs |
| Page service | `subconscious/meal_page_service.py` | `/meals` view-model + Generate button |
| Agents | `agents/{daily_meal_proposer, weekly_meal_planner, weekly_meal_planning, meal_research, meal_context_distiller, grocery_intent_scanner, feedback_extractor}` | LLM decisions |
| Managers | `multi_agents/{meal_research_manager, weekly_meal_planning_manager}` | research detour + planning chain |
| Surface | `routes/meals.py`, `templates/meals.html` | weekly grid + shopping list + comment boxes |

### Data stores

- **Pods**: `plan.weekly_meals` (weekly grid), `intention.meal` (per proposal),
  `intention.shopping` (ad-hoc run), `intention.meal_set` (per-run summary),
  `feedback.comment` (a user comment awaiting extraction).
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
  → meal_context_builder._build_food_beliefs()  (recency lane, below)
  → distiller → planner            # next plan reflects the feedback
```

Three properties that are easy to get wrong and are pinned by guard tests:

### 3.1 Recency lane — fresh feedback must not be outranked

`_build_food_beliefs()` feeds the planner two lanes:

1. **Established lane** — food-domain beliefs ranked by `current_net_weight DESC`, top-N.
2. **Recency lane** — food beliefs with a `user_comment` evidence row in the last 21 days,
   surfaced **ahead** of the established lane, deduped.

The recency lane exists because ranking by weight alone buries fresh feedback: a
brand-new belief has `current_net_weight = NULL` (only the nightly recompute populates
it) and even once weighted sits far below the established cutoff. Without the recency
lane, a just-stated preference never reaches the planner. The lane keys on **evidence
recency**, not `last_confirmed` (the nightly pipeline bumps `last_confirmed` broadly,
which would flood the lane), and it **excludes contradicts-only beliefs** so a preference
the user just refuted doesn't reappear as current intent.

### 3.2 Beliefs orphan unless their domain is registered

The belief recompute step is **domain-scoped**: the nightly `belief_engine` pipeline only
processes domains listed in `configs/belief_domains.yaml`. A belief written under a domain
that isn't listed never gets a weight, never decays, and never has contradictions
reconciled. Any domain the meal feedback path writes into must be present in that file.
(A domain added solely for recompute coverage can use empty `tags`/`ticket_types` so it
runs recompute/reevaluate without spinning up an LLM derivation lane.)

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
- `test_meal_food_beliefs_recency.py` — a fresh, low/NULL-weight belief reaches the planner
  via the recency lane even when outranked; a contradicted-only belief does not.
- `test_feedback_extractor_no_phantom.py` — a `contradicts` against a non-existent belief
  mints nothing; against an existing belief it weakens, not creates.

---

## 5. Surfaces & cadence

- `/meals` (`routes/meals.py`, `templates/meals.html`): weekly grid, shopping list (the
  live Google Doc body + ad-hoc `intention.shopping` items), and per-day/week/shopping
  comment boxes that feed the learning loop.
- Routines (`configs/routines/public/`): weekly planner (Sunday), daily proposer (early
  morning), grocery sync (before the daily proposer), feedback extractor (daily). The
  nightly `belief_engine` pipeline recomputes belief weights and reconciles contradictions.

---

## 6. Genuinely well-built (fair credit)

Pod-mediated weekly→daily decoupling; strict, well-documented Pydantic schemas with a
coercion net; sensible model tiering; the closed feedback loop (correctly timed so
overnight feedback lands in the morning plan); two-tier KG/belief food-keyword filtering
with runaway-density surfacing; idempotency design (inventory-applied markers, scanned
chat-id state); the distiller pattern; solid `/meals` week navigation.
