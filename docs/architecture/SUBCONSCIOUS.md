# Subconscious

The Subconscious is EmiOS's autonomous background "mind." It runs while the user is away, maintains a durable **concerns register**, turns concerns into two kinds of output — **proposals** (intention pods) and **questions** (the pending-questions queue) — surfaces a daily **digest**, and feeds the **proactive outreach** channel. Nothing it produces acts on the world directly: the noticer *observes and delegates* (routing concerns to dayflow / proposers via `addressable_by`), proposers *propose* (pods the user comments on), and high-stakes questions become tickets the user answers.

> Cross-refs: proactive outreach is a Dayflow pipeline stage — see [05_DAYFLOW.md](05_DAYFLOW.md). Proposal/feedback artifacts are pods — see [14_PODS.md](14_PODS.md). The feedback→belief sink writes the belief store — see [16_BELIEF_ENGINE.md](16_BELIEF_ENGINE.md). Meal planning is the deepest proposer lane — see [MEAL_PLANNING.md](MEAL_PLANNING.md). Routine plumbing — see [06_PIPELINES_AND_ROUTINES.md](06_PIPELINES_AND_ROUTINES.md).

## 1. Mental Model

```
                  resources/subconscious/resource_concerns_register.json
                        (active / addressing / resolved / dormant)
                                        ▲ │
                  reinforces / resolves │ │ reads active+addressing
                                        │ ▼
  context_builder ──► subconscious::noticer ──► persist.apply_noticer_output
   (24 pre-injected      (LLM, no tools)          │  ├─ concern CRUD (in-place)
    context items)                                │  ├─ enqueue pending_questions
                                                  │  └─ tick log (jsonl audit)
                                                  │
   proposers (meal/wellness/romantic) read active+addressing concerns whose
   addressable_by names them ─► mint intention.* pods
                                                  │
   scheduler_arbiter aggregates intention.* ─► one plan.weekly_schedule pod
                                                  │
   digest_runner renders register ─► master_room chat row (no LLM)
                                                  │
   pending_question queue ──► two delivery bridges ──► user reply
       │                       (in-chat nudge, conversation_starter fast-path)
       └─► answer_capture ──► subconscious::answer_matcher ──► mark_answered
                              ──► annotate_concern ──► trigger_noticer (cooldown)

   dayflow work-object closure (concern-linked) ──► concern_feedback.propagate_work_outcome
       ──► persist.apply_work_outcome (done→addressing; user-declined→DORMANT with the
           user's verbatim words; system-abandon→journal only) ──► trigger_noticer (cooldown)
```

The register is the **spine**: every other component either feeds it (noticer, answer-capture, work-outcome propagation) or reads it (proposers, digest, dashboard). The noticer is the only LLM that mutates concern lifecycle — the propagation writers are deterministic id-joins (2026-08-01: the evaluator cites `[concern:<prefix>]` ids in `based_on`, work objects carry `constraints.concern_refs`, and every dayflow closure path back-propagates; before this, 19 duplicate AC-service work objects were minted because outcomes never reached the register).

### Dispatch & scope

Routines invoke each lane through one shared helper, `_run_subconscious_agent(...)` in `app/assistant/routine_handlers/subconscious.py`. It builds the scope, creates the agent via `DI.agent_factory`, invokes `agent.action_handler(Message(agent_input=context, scope_context=scope))`, and returns `result.data`. It **raises on agent failure** so `routine_manager` records the failure and counts it toward `on_error.max_failures`.

Scope comes from `app/assistant/subconscious/scope.yaml`, loaded as `load_scope_for_source(kind="subsystem", source_id="subconscious", actor_id=..., identity_overrides={owner_id:"system", scope_id:"subconscious::<lane>", surface:"internal"})`. It is **authority 99** (autonomous routine — no human approver, so 99 removes approval friction; 100 is reserved for the courier band), `tools: [all]`, `resources: [all]`. Tools default to `all` because the meal lane invokes managers (`weekly_meal_planning_manager`, `meal_research_manager`) that rely on their own native `allowed_tools`; narrowing here would strip them. The noticer and proposer agents themselves declare `allowed_tools: []` — they are fully pre-injected.

## 2. Concerns Register (the spine)

`resources/subconscious/resource_concerns_register.json` — four buckets plus metadata:

| Bucket | Meaning |
|--------|---------|
| `active` | Live concerns. **The only bucket proposers read** for routing. |
| `addressing` | Work is in flight (e.g. dayflow already researched it) but not resolved (no booking yet). Moved out of `active` so it stops nagging the planner, still tracked. |
| `resolved` | Done — carries `resolved_at_utc`, `resolution_reason`, `resolution_evidence`. |
| `dormant` | Accepted-chronic patterns; compacted (founding + freshest evidence only). |

A concern carries: `concern_id`, `title`, `subject`, `kind`, `domain_tags`, `severity` (low/medium/high), `horizon` (today/this_week/this_month/long_horizon), `evidence[]`, `addressable_by[]` (e.g. `["dayflow_orchestrator", "wellness_proposer"]`), `notes`, `first_observed`, plus lifecycle bookkeeping (`reinforcement_count`, `reinforcement_notes` journal, `last_reinforced_utc`, `addressing_since_utc`, `last_disposition_at_count`, `escalation`). `ConcernKind` ∈ `{pattern_drift, anticipated_need, opportunity_external, opportunity_internal, schedule_collision, gift_opportunity}`.

### Lifecycle pressure

`persist.compute_pressure(register)` is a **pure deterministic read** that forces the noticer to make a decision on long-running concerns. It returns `{needs_disposition, addressing_stale}`:

- **`needs_disposition`** — any active/addressing concern where `reinforcement_count − last_disposition_at_count ≥ DISPOSITION_REINFORCEMENT_THRESHOLD` (8). Stops eternal "reinforcement sink" concerns that accrete evidence forever with no decision.
- **`addressing_stale`** — any `addressing` concern with `addressing_since_utc` older than `ADDRESSING_STALE_DAYS` (4) — the handoff likely dropped.

The context builder injects these under a **"CONCERNS UNDER PRESSURE — disposition REQUIRED this tick"** prompt block; the LLM must emit exactly one `concern_dispositions` entry per pressured id:

- `accept_chronic` → moves to `dormant`, sets `chronic=true`, archives evidence.
- `re_escalate` → back to `active` with a fresh high-urgency escalation marker; resets the pressure window.
- `keep_active` → justified continuation; resets `last_disposition_at_count` so the rule doesn't re-fire next tick.

Growth caps keep the register bounded: evidence is trimmed to head 3 + tail 9 (`_trim_evidence`), the reinforcement journal to the newest 10 (`_trim_journal`), with dropped counts recorded.

### Audit

Every tick appends one JSONL line `{tick_utc, output}` to `resources/subconscious/resource_subconscious_tick_log.jsonl` (the full AgentForm dump, for replay + the dashboard). The register itself stamps `last_updated_utc` / `last_noticer_tick_utc`.

## 3. The Noticer Loop

The single LLM that runs the "thinking." Agent `subconscious::noticer` (`app/assistant/agents/subconscious/noticer/`): `gpt-5.6-luna`, **`allowed_tools: []`** (no tools — context is fully pre-injected; tool access is a planned v0.5 step).

**Context** — `context_builder.build_noticer_context(trigger_mode=...)` returns a flat dict, one string per `user_context_items` key. Every section builder **fails soft**, returning `"(no <kind> data available)"` rather than raising, because the prompt is written to handle partial context. Notable sources:

| Context item | Source |
|---|---|
| `recent_friction_signals` | `chat_cluster` pods' `metadata.friction_signal`, grouped by `(subject, kind)` over 14d; self-aliases (`self`/`me`/first-name) collapse to one bucket so the 3+ pattern threshold fires. |
| `concerns_register_active` | The register's active+addressing, plus the injected pressure block. |
| `question_mailbox` | `pending_questions.get_for_noticer_processing()` — captured answers + stale-asked questions awaiting an outcome. |
| `dayflow_recent` | Recent `dayflow_item` rows from `unified_log_2026`, filtered to signal. CAVEAT (2026-08-01 audit): post-WO-cutover the item lane carries intake only — no `Result:` outcome lines exist anymore, so the ADDRESSED disposition cannot fire from this input; work outcomes now reach the register via closure propagation instead (§work-outcome path). Repointing this builder at work-store terminals is an open repair. |
| `recent_chat_clusters`, `recent_passing_mentions` | 48h chat clusters; 60–220-char un-clustered user utterances. |
| `calendar_today_tomorrow` / `_week_summary` / `_30_90d` | Three calendar windows via the `get_calendar_events` tool (Pass B reads 8–90d). |
| `watchlist_summary`, `recurring_obligations`, `family_graph_digest` | User-curated `resource_subconscious_watchlist.md` / `resource_recurring_obligations.md`; KG cards + ≤90d birthdays for non-household important_people. |
| `kg_household_digests`, `family_roster` | Per-member KG entity-card descriptions; household resolved from `resource_user_data.json` important_people with household relationships. |

**Two passes** (framed in `system.j2`): **Pass A inward** — verify existing concerns, process the forced dispositions, drain the question mailbox, detect new pattern_drift from the friction aggregate. **Pass B outward** — mandatory anticipated-need calendar scan + optional opportunity/gift scouting off `family_graph`/`watchlist`.

**Output** (`agent_form.AgentForm`): `new_concerns`, `reinforced_concerns`, `addressing_concerns`, `resolved_concerns`, `escalated_concerns`, `concern_dispositions`, `question_outcomes`, `belief_updates`, `pending_questions`, plus `summary` / `skipped_pass_b` / `skipped_pass_b_reason`.

**Persist** — `persist.apply_noticer_output(output)` applies each list to the register in order (new→active, reinforce in-place, active→addressing, resolve, escalate, dispositions, question outcomes), trims caps, appends the tick-log line, then enqueues `pending_questions` into the queue. `belief_updates` are recorded in the tick log only (not yet written to the belief store).

**Triggers** — routine `subconscious_noticer` → `noticer_run` (interval 86400 in a 04:00–22:00 window — note: interval semantics make the run time DRIFT a few minutes later each day rather than anchoring at 04:00; observed ~10:45 local after a week), plus an ad-hoc tick fired by answer-capture (cooldown-guarded; see §5).

## 4. Pending-Questions Store + Two Delivery Bridges

A simple SQLite queue of questions the assistant wants to ask — the substrate for **all** proactive data-gathering, not just the noticer's.

**Model** — `app/assistant/database/pending_question.py`, table `pending_question`. Fields: `question_text`, `topical_tag` (free string, e.g. `meals`/`family`/`wellness`/`general`), `priority` (low/medium/high), `status`, `related_concern_id`, `ask_mode` (`chat` | `ticket`), answer-capture columns (`answered_at`, `answer_text`, `answer_message_id`), `created_by`, `expires_at`, `asked_at`, `asked_in_message_id`. Lifecycle:

```
pending ──► asked ──► answered ──► closed       (noticer processed the answer)
   │          └─────► (stale) ───► expired       (no answer in window; stated default applied)
   ├──► dismissed   (cancelled before ask)
   └──► expired     (freshness window passed before it was asked)
```

**Store API** — `app/assistant/pending_questions/store.py` (sessions are short-lived, never held across I/O): `enqueue_question` (3-day default expiry), `get_pending` (pick order: priority then oldest), `mark_asked`, `get_asked_unanswered`, `get_for_noticer_processing` (drain-your-own — the noticer only sees questions it `created_by`), `mark_answered`, `close_question`, `mark_dismissed`, `expire_stale`, `count_asked_in_window`. Re-exported from the package `__init__`.

The noticer's `_enqueue_pending_questions` derives each row from its related concern: `topical_tag` = concern's first `domain_tag`, `priority` = severity, `expires_after_hours` = horizon-mapped (today/this_week → 24h/7d, long_horizon → never). `if_unanswered` is folded into the question text as a "(if no reply, I'll go with: …)" tail. **Routing rule**: `ask_mode="ticket"` only when `severity=="high"` and horizon ∈ `{today, this_week}`; everything else is `chat`.

### Bridge 1 — in-chat nudge (the default)

`pending_questions.injector.pick_question_for_nudge(topic_tag=...)` picks the best candidate (topic match → priority → age), marks it `asked`, returns `(id, text)`. Budget: `DEFAULT_DAILY_BUDGET=6` asked/24h and `DEFAULT_MIN_MINUTES_BETWEEN=10` anti-back-to-back; **`high` priority bypasses both gates**. Both gates count by `asked_at` regardless of current status.

**Every surfaced ask carries an ANCHOR** — `asked_in_message_id`, the unified_log row id of the message the ask rode with. Answer capture resolves the asked ROOM from the anchor; an anchor-less ask is skipped by capture and can only expire. Each bridge supplies its own: the chat nudge passes this turn's inbound user-message row id (threaded onto the blackboard by the room session manager as `inbound_message_id`); proactive surfaces anchor AFTER emitting via `set_ask_anchor(question_id, asked_in_message_id=<their outbound row id>)` (first anchor wins).

Wired into the **chat reply path** via the context injector: `app/assistant/agent_runtime/services/context_injector.py` resolves the `chat_nudges` context key by calling `pick_question_for_nudge(topic_tag=None)` and setting `context["chat_nudges"] = question_text`. `master_room::chat_gate` declares `chat_nudges` in its `user_context_items` and renders it in `prompts/user.j2` under a "GOOD TO BRING UP … IF IT FITS NATURALLY" header. It is a **soft hint** — the agent decides whether to weave it into its natural reply; the question is never mechanically appended.

### Bridge 2 — conversation_starter fast-path

The Dayflow conversation_starter stage (§6) also calls `pick_question_for_nudge()` as a **fast-path**, surfacing the highest-priority queued question proactively (before the user opens a chat) via `_emit_to_user`. Same injector, same budget/dedup. This is preferred over the generic LLM starter because these are real data-gathering asks. After recording the outbound message it anchors the ask to that row (`set_ask_anchor`), so the reply is captured like any chat-asked question.

### Ticket delivery (high-stakes)

`pending_questions.ticket_delivery.deliver_question_as_ticket(...)` (called from the noticer's enqueue path for `ask_mode="ticket"`) creates an `ask_user` ticket, marks it proposed, publishes a `proactive_suggestion` event, and flips the question to `asked` with `asked_in_message_id="ticket:<id>"` — which also removes it from the chat injector's pool (the `ticket:` anchor makes the chat-side matcher skip it). On failure it stays `pending` and the chat injector delivers it normally. `register_ticket_answer_listener()` (called once at boot, `initialize_system.py`) subscribes to `dayflow_ticket_responded`: an answered ticket routes into the same answer loop (mark answered → journal concern → trigger noticer); a dismissed ticket expires the question immediately.

## 5. Answer-Capture Loop

`app/assistant/subconscious/answer_capture.py` closes the ask→answer loop. Both triggers call `check_open_questions(room_id=...)`:

- **Per-turn** — `RoomSessionManager._maybe_check_question_answers(room_id=...)` runs after a user message. It cheap-gates on `get_asked_unanswered(limit=1)`, then spawns a daemon thread (`answer-capture-{room_id}`) so the check is **off the reply path**.
- **Hourly sweeper** — routine `subconscious_answer_sweep` → `answer_sweep_run` → `check_open_questions()` (unscoped), catching answers given after restarts or odd timing.

`check_open_questions` is **free in the common case**: no asked-unanswered questions → pure SQL, no LLM. For each open question it pulls candidate user messages in the asked room since `asked_at` (cap 12), and judges them with one `subconscious::answer_matcher` call (`gpt-5.4-mini`, no tools). The matcher returns `verdict` ∈ `{answered, partial, no_answer}` + `answer_text` / `answer_message_id` / `confidence` / `notes`; it biases to `partial`+low-confidence when unsure because a wrong captured answer corrupts the concern it routes back to.

On `answered`: `mark_answered` → if `related_concern_id`, `annotate_concern_answer` journals the answer onto the concern immediately (atomic write) → after the loop, `trigger_noticer(...)` fires one background noticer tick, guarded by a **600s monotonic cooldown** (the tick reads all captured answers anyway), so the register reacts within minutes instead of at the next daily tick. Work-outcome propagation (2026-08-01) fires the same trigger when a concern-linked work object closes.

## 6. Proactive Outreach

**Proactive outreach is a Dayflow pipeline stage, not a subconscious routine.** `app/assistant/pipelines/dayflow/steps/conversation_starter_stage.py` (`ConversationStarterStep`, `step_id="conversation_starter"`), configured by `app/assistant/pipelines/dayflow/step_configs/config_step_conversation_starter.json`. See [05_DAYFLOW.md](05_DAYFLOW.md).

`should_run` is a **five-veto gate** (all must pass), in order: `_quiet_hours_veto` → `_presence_veto` (AFK) → `_calendar_veto` (meeting now / event within N min) → `_rate_limit_veto` (`max_per_day`, `min_interval_minutes`) → `_random_gate` (per-bucket deterministic probability). On top of that, `run()` applies a **user-frequency gate** read from `resources/assistant/assistant_core.json` `conversation_starter.frequency` (off/rare/regular/frequent → min-interval), settable in the `/settings/assistant` UI; `off` disables entirely.

Once gated open, `run()` resolves a starter by **priority**:

1. **Context-activation memo** — if the context engine prepared an unaddressed memo, surface its first suggested question.
2. **Pending-question fast-path** — `pick_question_for_nudge()` (Bridge 2 above).
3. **LLM starter** — agent `conversation_starter` (`gpt-5.1`, no tools) over heavy context (calendar, KG random topic, `life_gaps`, recent chat). Output `AgentForm`: `should_initiate`, `reason`, `intent`, `topics`, `starter_text`.

The chosen line is emitted to `master_room`, recorded to the global blackboard + `unified_log_2026` (as a `proactive` assistant message), logged to a per-day JSONL, and counted against the daily cap via `resource_conversation_starters_latest.json` (with a `recent_starters` dedup ring).

Config knobs (current): `max_per_day=8`, `min_interval_minutes=45`, `randomization.probability=0.6`, `presence_veto.veto_if_afk=true`, `calendar_veto.veto_if_event_starts_within_minutes=10`, `quiet_hours_local=[]`.

## 7. The Digest

A daily "what I've been noticing" message — **pure Python templating, no LLM** (the noticer's 04:00 tick already did the thinking; the digest is the voice).

`digest_runner.run_digest_pass(room_id="master_room")` loads the register + `resource_digest_state.json` + the last tick's `pending_questions`, renders via `digest_builder.render_digest`, writes `app/subconscious_digests/digest_YYYY-MM-DD.md`, persists an assistant row to `unified_log_2026` (`source="subconscious_digest"`, so it appears in master_room history regardless of live sockets), and pushes to live SocketIO subscribers via `DI.outbound_chat_publisher`.

`render_digest` sections: **New this round** (active/addressing not in `previously_surfaced_concern_ids`, multi-line, severity-sorted), **Still tracking** (one-liners), **Resolved** (≤5 most-recent), and up to **2 pending questions** (rendered on quiet days too — a quiet digest is a natural moment to ask). Deliberately omits belief updates, dormant concerns, full evidence, and reasoning. After rendering, the state file keeps the currently-active concern ids as `previously_surfaced_concern_ids` so they're "ONGOING" next time. **The digest is a third delivery bridge**: questions come from the pending_question store (`get_pending(limit=2)`), and after the digest row persists they are marked `asked` anchored to that row — the user's later reply in master_room goes through the same answer capture as a chat-nudged ask. Preview runs (`post=False`) render without consuming anything. Routine `subconscious_digest` → `digest_run` (interval 86400, window 07:30–22:00).

## 8. Proposer / Distiller Lanes

Daily, the noticer's concerns fan out to **proposer** agents, which mint `intention.*` pods; a **scheduler arbiter** then synthesizes them into one weekly plan. Each lane has a `<lane>_context_builder.py` (pre-injects context — all agents are toolless), a `<lane>_persist.py`, and a routine handler. The proposer agents are **bare top-level agents** (`name: wellness_proposer`, not `subconscious::…`); only the noticer and answer_matcher are namespaced under `subconscious/`.

| Lane | Agent (model) | Reads | Writes |
|------|---------------|-------|--------|
| **Daily meal** | `daily_meal_proposer` (`gpt-5-mini`) | latest `plan.weekly_meals`, inventory, beliefs, easy-meals rotation, `addressable_by=["meal_proposer"]` concerns, weekly schedule | `intention.meal` (per meal), `intention.shopping`, `intention.meal_set` |
| **Wellness** | `wellness_proposer` (`gpt-5-mini`) | wellness/general calendar, `addressable_by=["wellness_proposer"]` concerns, recent `intention.wellness`, weekly schedule | `intention.wellness`, `intention.wellness_set` |
| **Romantic** | `romantic_proposer` (`gpt-5-mini`) | key dates (birthdays/anniversary from `resource_user_data.json`), food/wellness calendars, `addressable_by=["romantic_proposer"]` concerns, weekly schedule | `intention.romantic`, `intention.romantic_set` |
| **Arbiter** | `scheduler_arbiter` (`gpt-5.1`) | all `intention.{meal,wellness,romantic}` in [today, +14d], previous `plan.weekly_schedule`, household calendar (hard constraints), key dates | **one** `plan.weekly_schedule` pod |
| **Skill distiller** | `skill_distiller` (`gpt-5.1`) | a week of `intention.*` + `plan.weekly_schedule` + experience `chat_cluster`s + the house-rules resources | appends to `resource_learned_skills_proposed.md` (**no auto-apply, no pods**) |

All proposers read the arbiter's latest plan back through `context_builder.build_weekly_schedule_block()` (the "honor last week's plan" loop): `is_anchor` items are **locked constraints**, non-anchor items are flex.

**Arbiter** (`scheduler_arbiter_persist.apply_scheduler_arbiter_output`) is the single weekly source of truth. It mints one `plan.weekly_schedule` pod (`for_agents=[meal_proposer, daily_meal_proposer, weekly_meal_planner, wellness_proposer, romantic_proposer]`, schedule grouped by day in the body, `is_anchor` markers). Conflicts it can resolve land in `conflicts_resolved`; the rest go to `conflicts_for_user`, each surfaced as a **dayflow ticket** via the `create_dayflow_ticket` tool (`suggestion_type="scheduler_conflict"`). The next arbiter run reads the answered ticket and re-resolves.

**Skill distiller** is long-loop learning: it reviews a week of proposer output, arbiter decisions, and outcome chat, and **proposes** rule additions to canonical house-rules / priority-rules files — appending a markdown block to `resources/subconscious/resource_learned_skills_proposed.md` for manual review. It never auto-applies and mints no pods.

**Easy meals** (`easy_meals.py`) is a structured "go-to dishes the family tolerates" registry in `resources/subconscious/resource_easy_meals.json` (each dish has a `max_days` cadence), seeded on first run from the markdown food registry. "Days since last served" is **derived** (not stored) by token-matching dish names against recent `plan.weekly_meals` + `intention.meal` pods (past dates only). `render_easy_meals_for_planner()` (status: never/due/resting, most-overdue first) is injected into both meal agents.

> **Note (resolved):** `wellness_persist._mint_intention_wellness_pod` and `romantic_persist._mint_intention_romantic_pod` once dropped every proposal pod silently via a `NameError` on an undefined local `addresses` inside a try/except — the same defect the meal lane hit before it was switched to fail-loud (`scratch/MEAL-PLANNING-AUDIT.md`). **Fixed:** all three lanes now mint their pods correctly.

### Daily ordering

All daily lanes are `runner: function` routines under `configs/routines/public/`, scheduled by `interval` (86400s) + an `active_window` whose `from` time is the effective fire time:

```
04:00  subconscious_noticer            (the day's thinking)
04:00  feedback_extractor_daily        (drain yesterday's comments → beliefs, window 04:00–05:00)
04:30  subconscious_grocery_sync       (refresh inventory snapshot, window 04:30–05:00)
05:00  subconscious_daily_meal_proposer  ┐
05:00  subconscious_wellness_proposer    ├ three proposers (window 05:00–05:30)
05:00  subconscious_romantic_proposer    ┘
05:30  subconscious_scheduler_arbiter   (after the proposers, window 05:30–06:00)
07:30  subconscious_digest              (render register, window 07:30–22:00)

hourly  subconscious_answer_sweep       (07:00–22:00; free when no open questions)
hourly  subconscious_meal_feedback      (07:00–21:00; ask "how was <dish>?" + ingest replies)
weekly  subconscious_weekly_meal_planner   Sun 17:00  (set the upcoming week)
weekly  subconscious_skill_distiller       Sun 22:00  (long-loop learning)
```

All share `on_error: {max_failures:3, then:"disable_with_ticket"}` with backoff + `auto_retry_after_seconds` self-heal.

## 9. The /subconscious Page

`app/routes/subconscious.py` (`subconscious_bp`). `GET /subconscious` renders two things: the proposers' **output** (upcoming `intention.*` pods grouped by date, with per-item comment boxes) and the **mind overview** (`dashboard_service.load_mind_overview` → the concerns register sliced by bucket, recent tick activity from the tick log, and the latest digest).

`POST /subconscious/comment` is the **universal comment endpoint** — every surface (this page, `/meals`, future per-domain pages) POSTs `{target_pod_id, text, target_scope?}` here. It mints a `feedback.comment` pod (`feedback_service.mint_feedback_comment`, tags `["feedback","user_comment","unprocessed"]`, `for_agents=["feedback_extractor"]`) and returns the refreshed comment list.

**Feedback → belief sink**: the `feedback_extractor` agent (`gpt-5.1`, routine `feedback_extractor_daily`) drains unprocessed `feedback.comment` pods (`feedback_service.list_recent_unprocessed_comments`), extracts belief updates, writes them to the belief store (`upsert_belief` / `add_evidence_to_existing`), and stamps each pod `processed_at_utc` + flips its tag to `processed`. A `contradicts` signal never mints a new belief (only weakens an existing one; otherwise recorded as `phantom_skipped`). This closes the loop: comment → extractor → belief → next-day proposer reads the belief snapshot. See [16_BELIEF_ENGINE.md](16_BELIEF_ENGINE.md).

## 10. Data Stores & Pod-Kind Reference

### Files (`resources/subconscious/`)

| Path | Writer | Role |
|------|--------|------|
| `resource_concerns_register.json` | `persist`, `answer_capture` | The spine (active/addressing/resolved/dormant). |
| `resource_subconscious_tick_log.jsonl` | `persist` | Append-only audit (one tick per line). |
| `resource_digest_state.json` | `digest_builder` | `previously_surfaced_concern_ids`, `last_digest_at_utc`, `history_count`. |
| `resource_subconscious_watchlist.md` | user | Pass B external-scouting input. |
| `resource_recurring_obligations.md` | user | Pass B anticipated-need input. |
| `resource_learned_skills_proposed.md` | `skill_distiller` | Proposed rule additions (manual review). |
| `resource_easy_meals.json` | `easy_meals` (UI) | Go-to-dish registry (gitignored — food PII). |
| `resource_meal_feedback_state.json` | `meal_feedback_runner` | Asked-meal dedup + question→pod map. |
| `app/subconscious_digests/digest_*.md` | `digest_runner` | Rendered daily digests. |

SQLite: `pending_question` table (`app/assistant/database/pending_question.py`). The belief store (`user_beliefs` / `belief_evidence`) is written by `feedback_extractor`.

### Pod kinds (two-axis: kind = handling class, variant tag = routing handle)

| Kind + variant | Minted by | Consumed by |
|------|-----------|-------------|
| `intention` #meal / #shopping (+#run_summary per run) | `daily_meal_proposer` | arbiter, dashboard, skill_distiller, easy_meals |
| `intention` #wellness (+#run_summary) | `wellness_proposer` | arbiter, dashboard, skill_distiller |
| `intention` #romantic (+#run_summary) | `romantic_proposer` | arbiter, dashboard, skill_distiller |
| `plan` #weekly_meals | `weekly_meal_planner` | `daily_meal_proposer`, easy_meals |
| `plan` #weekly_schedule | `scheduler_arbiter` | all proposers (`build_weekly_schedule_block`) |
| `feedback` #comment | `/subconscious/comment`, `meal_feedback_runner` | `feedback_extractor` |
| `chat_cluster` | upstream (pod classifier) | noticer (friction signals), skill_distiller |
| `exploration_attempt` | upstream | noticer (`exploration_outcomes_30d`) |

Consumers query `store.query(kind="intention", tags=["meal"])`; a family pod
must carry exactly one variant tag (enforced at `PodStore.put`). Run-summary
pods carry the domain variant plus `#run_summary` and no `metadata.date`, so
item consumers (which key on date/dish metadata) pass over them naturally.
All pod ids follow `datapod:<kind>:<uuid4-hex-24>`. See [14_PODS.md](14_PODS.md).

## Key Files

| File | Purpose |
|------|---------|
| `routine_handlers/subconscious.py` | All lane dispatchers + `_run_subconscious_agent` shared helper |
| `subconscious/scope.yaml` | Authority-99 permission scope for the subsystem |
| `subconscious/context_builder.py` | Noticer context (24 items) + shared `build_weekly_schedule_block` |
| `subconscious/persist.py` | `apply_noticer_output`, `compute_pressure`, register CRUD, question enqueue |
| `subconscious/answer_capture.py` | `check_open_questions`, judge, `annotate_concern_answer`, `trigger_noticer` |
| `subconscious/digest_runner.py` / `digest_builder.py` | Digest pass + pure-Python renderer |
| `subconscious/scheduler_arbiter_*.py` | Arbiter context + `plan.weekly_schedule` mint + conflict tickets |
| `subconscious/{wellness,romantic,meal}_*.py` | Proposer context builders + persisters |
| `subconscious/skill_distiller_*.py` | Weekly rule-proposal lane |
| `subconscious/feedback_service.py` | `feedback.comment` mint/fetch + dashboard intention list |
| `subconscious/dashboard_service.py` | `/subconscious` mind-overview read layer |
| `pending_questions/{store,injector,ticket_delivery}.py` | Question queue CRUD, chat-nudge picker, ticket delivery |
| `database/pending_question.py` | `PendingQuestion` model + lifecycle |
| `agents/subconscious/{noticer,answer_matcher}/` | The two namespaced LLM agents |
| `agents/{wellness,romantic,daily_meal,...}_proposer/`, `agents/scheduler_arbiter/`, `agents/skill_distiller/`, `agents/feedback_extractor/`, `agents/conversation_starter/` | Bare-name lane agents |
| `pipelines/dayflow/steps/conversation_starter_stage.py` | Proactive outreach (Dayflow stage, not a routine) |
| `routes/subconscious.py` | Page + universal comment endpoint |
| `configs/routines/public/subconscious_*.json`, `feedback_extractor_daily.json` | Routine schedules |
