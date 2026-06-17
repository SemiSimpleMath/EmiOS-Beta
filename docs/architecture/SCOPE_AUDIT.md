> **⚠️ ARCHIVED / HISTORICAL (2026-06-17).** Investigation record of the emi_team→http_manager starvation bug. That bug is **fixed** — via the "a granted manager keeps its whole own tool surface" *ceiling* model (`scope_adapter._apply_manager_narrowing`), not the pure authority-cap this audit proposed (`_intersect_allowed_tools` is still live, not dead as predicted). For current scope behavior see **[SCOPE.md](SCOPE.md)**. Kept for history.

---

# Scope Audit (2026-05-28)

**Status:** investigation document. **No code changes proposed yet.** Companion to `15_EMI_TEAM_AND_SCOPE.md`.

This audit was kicked off after a concrete failure: emi_team → http_manager delegation stripped `http_request` from http_team::planner's tool list, while the same managers' direct delegation (master_room → http_manager) worked fine. The shape of that failure suggested a wider scope question rather than a one-place patch; the user explicitly asked for an architectural audit, not a fix.

The audit covers ScopeContext's data model, every construction site, the inheritance mechanism at the manager boundary, the historical fix from 2026-05-05 (commit `0a60f8e5`), and a per-field behavior table. The current state turns out to be **architecturally correct in intent but with a hidden coupling that causes silent failure for managers that don't declare a redundant config field.**

---

## 1. Data model (ScopeContext)

Defined in `app/assistant/utils/pydantic_classes.py:109`. All sub-policies inherit `ScopeBaseModel` (`extra="forbid"`).

### Top-level identity fields

| Field | Type | Nominal purpose |
|---|---|---|
| `schema_version` | Literal | Versioned wire-format marker. Currently `"scope_context_v1"`. |
| `scope_id` | str | Unique id for this scope instance. Derived per-invocation. |
| `owner_id` | str | The principal whose data this scope reads from. |
| `actor_id` | str | The principal whose actions this scope can take. |
| `surface` | str | Originating transport (ui / slack / sms / telegram / system). |
| `room_id` | Optional[str] | Conversational room this scope binds to. |
| `room_context_id` | Optional[str] | Sub-context within the room (e.g. specific Slack thread). |
| `visibility` | Literal | `owner_only` / `room_shared` / `global_shared`. |
| `policy_id` | Optional[str] | Reference to a room policy doc that produced this scope. |
| `acting_as` | str (default `"user"`) | Principal mode — "user" (the user), "emi" (the assistant on her own behalf), or future principals. Stamped at chat ingress by keyword detector or explicit `/actas`. |
| `reply_to` | Optional[Dict] | Surface routing back to the originating chat. |

### Sub-policies (12 of them)

| Field | Class | Notes |
|---|---|---|
| `history` | ScopeHistoryPolicy | mode / source / lookback / max_messages / max_chars |
| `resources` | ScopeResourcePolicy | allowed_global_resources, allowed_room_resources, denied_resources, resource_groups |
| **`tools`** | **ScopeToolPolicy** | **allowed_tools, blocked_tools, requires_approval_tools, allow_external_side_effects. THIS IS THE BUG SITE.** |
| `entities` | ScopeEntityPolicy | allowed_entity_cards, pinned_entities, lookback caps |
| `cards` | ScopeCardPolicy | allowed_cards, max_cards_per_turn, max_total_chars |
| `writes` | ScopeWritePolicy | write_unified_log, write_kg, allow_fact_extraction, writable_state_keys |
| `delivery` | ScopeDeliveryPolicy | auto_send, allow_initiation, allowed_reply_types |
| `approval` | ScopeApprovalPolicy | authority_level (0-100, ge/le validated) |
| `retention` | ScopeRetentionPolicy | persist_chat, persist_tool_results, allow_context_summarization, redact_before_persist |
| `execution` | ScopeExecutionPolicy | max_turns, max_tool_calls, timeout_seconds, allowed_models |
| `delegation` | ScopeDelegationPolicy | empty placeholder class — reserved for future delegation rules |
| `skills` | ScopeSkillsPolicy | always_inject, denied_skills — composes with auto-inject paths |

### Sub-policy default semantics for `tools`

`ScopeToolPolicy.allowed_tools` defaults to `["all"]`. This is the semantic marker for "no narrowing" — anything intersected against `"all"` returns the other side unchanged. A field validator (`validate_allowed_tools_semantics`) raises if you mix `"all"` with specific names.

---

## 2. Construction sites (40+)

ScopeContext is constructed in **40+ places** across the codebase. There is no central factory. Each site builds from scratch using `ScopeContext(...)` or `ScopeContext.model_validate(...)`. The sites fall into four tiers:

### Tier 1 — Entry boundary (architecturally load-bearing)

| File:line | Purpose |
|---|---|
| `room_session_manager/services/room_scope_builder.py:148` | Chat ingress from UI/socket/slack/sms/telegram. The canonical "scope from a user message" path. |
| `room_session_manager/services/system_scope_builder.py:70` | Helper for system-initiated scopes (no chat origin). |
| `manager_runtime/services/scope_adapter.py:91 (build_system_scope_context)` | Standalone scope builder used by routines, pipelines, orchestrator, task-runner. |
| `manager_runtime/manager_invoker.py:105` (`scope_adapter.apply`) | Called BEFORE every manager invocation. Runs `_resolve_scope_context` then `_apply_manager_narrowing`. This is the central adapter the May 5 fix lives in. |
| `manager_classes/MultiAgentManager.py:230, 247-261, 484-495` | Manager pulls scope off its inbound message and pushes to blackboard. Used by every loop cycle. |
| `lib/core_tools/manager_interface/manager_interface.py:47, 88` | **The inheritance point when a parent manager invokes a child manager-as-tool.** Reads `tool_message.scope_context` and stamps it verbatim on the child manager's incoming Message. |
| `control_nodes/tool_caller.py:112` | Validates scope before tool dispatch. The point where the parent's scope_context flows into ToolMessage. |
| `control_nodes/_tool_caller_util.py:66` | Helper used by other tool-call paths. |

### Tier 2 — Routine / pipeline entry points

Every routine that runs without chat context constructs its own scope from scratch using `build_system_scope_context()` or by calling `ScopeContext(...)` directly. There are 25+ such sites:

- `subconscious/run_*.py` (9 files — noticer, meal proposer, weekly planner, wellness, romantic, skill distiller, feedback extractor, arbiter, grocery sync)
- `subconscious/meal_page_service.py` (×2)
- `subconscious/run_weekly_meal_planning.py` (×2)
- `routine_handlers/feedback_extractor.py`, `routine_handlers/subconscious.py`
- `kg_investigator/finding_executor.py`, `finding_processor.py` (×2)
- `kg_maintenance_pipeline/step_edge_canon_curation.py`
- `wiki_generator/page_writer.py` (module-level + per-call), `lead_writer.py`, `consistency_critic.py`
- `importance/scoring.py`, `kg/edge_importance_eval.py`
- `kg_projection/sections.py`, `kg_projection/tagger.py`
- `kg_resolution/resolve_with_prose.py`
- `pod_store/image_pod_enrichment.py`
- `lib/task_utils/task_create_compile_runner.py`

These don't inherit from anywhere — they build a fresh ScopeContext per run.

### Tier 3 — Tool-level "courier" sub-scopes

Some tools construct sub-scopes for their internal sub-work. The pattern is `courier_scope = ScopeContext(...)` used to authorize a pod-resolution or sub-agent call inside the tool's execution.

- `lib/tools/execute_code/execute_code.py:473`
- `lib/tools/http_request/http_request.py:812`
- `lib/tools/web_type_secret/web_type_secret.py:110`
- `lib/tools/invoke_agent/invoke_agent.py:88`
- `lib/tools/oauth_token_refresh/oauth_token_refresh.py:281, 309`

### Tier 4 — Admin/UI/test scopes

`me/api.py:512`, `routes/preferences.py:461`, `dj_manager/scope.py`, `game/geoguessr/geo_screenshot_timer.py:250`, `email_utils.py:31` (module-level constant), `chat_scan.py`, `reasoning_agent.py`, `execution_trace/dojo.py`, `location_manager/location_manager.py:454`. These are typically scopes for one-off operations.

### Implication

The proliferation matters for any architectural shift: a change to ScopeContext semantics would have to be either backward-compatible at the data-model level, or every construction site would need touching. Today the 40+ sites mostly construct identity fields manually and accept defaults for the policy sub-objects, so most aren't affected by the policy-narrowing logic — only the Tier 1 paths exercise narrowing.

---

## 3. The inheritance mechanism, traced end-to-end

The failure case (emi_team → http_manager) flows through this exact path:

```
[emi_team::planner emits action='http_manager' with action_input={task, information}]
                                  │
                                  ▼
control_nodes/tool_caller.py
  _execute_tool_call(scope_context=emi_team's_scope)
  builds ToolMessage(scope_context=emi_team's_scope)
  invokes tool_instance.execute(tool_message)
                                  │
                                  ▼
lib/core_tools/manager_interface/manager_interface.py
  inherited_scope = getattr(tool_message, "scope_context", None)   # line 47
  ...
  manager_message = Message(scope_context=inherited_scope, ...)    # line 88
  DI.manager_invoker.invoke(http_manager_instance, manager_message)
                                  │
                                  ▼
manager_runtime/manager_invoker.py:105
  scoped_message = self.scope_adapter.apply(
      manager_name='http_manager',
      manager_config=http_manager_config,
      message=manager_message,
  )
                                  │
                                  ▼
manager_runtime/services/scope_adapter.py
  apply():
    scope = _resolve_scope_context(message)
        # message.scope_context is set → just validates and returns it
        # so `scope` = emi_team's scope, unchanged

    narrowed = _apply_manager_narrowing(scope, http_manager_config)
        # ← HERE is where the May 5 fix lives
        # Reads http_manager_config['scope_contract']
        # IF tools_cfg.allowed_tools exists: REPLACE scope.tools.allowed_tools with child's
        # IF tools_cfg.allowed_tools MISSING: leave parent's allowed_tools unchanged ← BUG
        # IF tools_cfg.blocked_tools exists: UNION with parent's blocked_tools
                                  │
                                  ▼
manager_classes/MultiAgentManager.py:218 (request_handler)
  scope_context = getattr(user_message, "scope_context", None)
  blackboard.update_state_value("scope_context", scope_context.model_dump())
  blackboard.update_state_value("scope_contract_enforced", True)
  tool_scope_service.initialize_scope(blackboard, tool_registry, manager_config, ...)
                                  │
                                  ▼
manager_runtime/services/tool_scope_service.py:395-420
  if scope_contract_enforced:
      scope_allow_set = set(scope_context.tools.allowed_tools)
      ranked = [t for t in ranked if t in scope_allow_set or t in always_show_set]
  # ← http_request stripped here because:
  #   scope.tools.allowed_tools = emi_team's narrowed list (no http_request)
  #   AND http_request not in http_manager's tool_visibility.always_show
  #     (wait — it IS in tool_visibility.always_show. See gap analysis below.)
                                  │
                                  ▼
http_team::planner receives Final allowed tools without http_request
http_team::planner has no legal way to make an http_request call
→ return_control
→ final_answer composes "I prepared but did not execute" report
→ emi_team treats it as success
```

---

## 4. The May 5 fix and its hidden coupling

Commit `0a60f8e5` (2026-05-05) addressed a structurally identical bug: Slack → web_manager left web::planner with `[ask_user, find_tool]` because room_manager's scope_contract.allowed_tools didn't enumerate every web tool. The fix changed `_apply_manager_narrowing` in `scope_adapter.py` to **REPLACE** allowed_tools with the child's contract (not intersect with parent's). Commit message articulates the principle explicitly:

> "Allowing a manager implicitly allows the manager's internal tool set. A manager's own scope_contract is the authority on its own leaves. Parent's allowed_tools list describes what the parent calls DIRECTLY — it shouldn't transitively narrow what a sub-manager can do internally."

This is the correct architectural intent and matches the principle the user just articulated.

### The hidden coupling

The replace-path only fires under this condition (line 435):

```python
if "allowed_tools" in tools_cfg:
    payload["tools"]["allowed_tools"] = allowed
```

Where `tools_cfg = manager_config['scope_contract']['tools']`.

So the fix REQUIRES that the child manager declare an `allowed_tools` key inside its `scope_contract.tools` block. Looking at `http_manager/config.yaml`:

```yaml
allowed_tools:          # top-level (the manager's own surface for what its planner can call)
  - http_request
  - oauth_token_refresh
  - ...

scope_contract:         # the contract presented to parents — separate field
  tools:
    blocked_tools:      # ← only blocked_tools declared here
      - emi_team_manager
      - http_manager
      - ...
    # NO allowed_tools key here
```

http_manager declares its `allowed_tools` at the top-level config (which the planner uses internally) but does NOT mirror them under `scope_contract.tools.allowed_tools`. The May 5 fix's `if` condition is False, so the replace branch is skipped, and the parent's narrowed allowed_tools list flows through unchanged.

`web_manager` (the manager the May 5 fix was tested against) presumably DOES declare `scope_contract.tools.allowed_tools` — which is why that path worked. The fix never covered the case where the child only declares `blocked_tools`.

### Why the always_show bypass also fails today

`tool_scope_service.py:415-420` says `ranked = [t for t in ranked if t in scope_allow_set or t in always_show_set]` — items in `always_show` SHOULD bypass the filter. http_manager's config includes:

```yaml
tool_visibility:
  always_show:
    - http_request
    - oauth_token_refresh
    - ...
```

But `tool_visibility.always_show` is `manager_config['tool_visibility']['always_show']`, NOT `scope_context.tools.allowed_tools`. They're separate fields read at different layers. The scope-contract filter in `tool_scope_service` doesn't consult `tool_visibility.always_show` — it consults the `always_show` parameter passed to `initialize_scope`, which is derived from the SAME manager_config. So in principle this SHOULD save http_request.

This needs one more layer of code verification (see open questions). If `always_show` is being correctly populated from the manager's own config, then http_request should be allowed-through even when scope_context.tools.allowed_tools excludes it. The fact that the log shows http_request stripped suggests either (a) always_show isn't being honored for this filter, or (b) always_show is being passed but is empty. Worth confirming before any fix.

---

## 5. Per-field propagation behavior table

For each ScopeContext field, three columns: should-propagate-into-sub-manager? does-propagate-today? gap?

| Field | Should propagate? | Does today? | Gap? |
|---|---|---|---|
| `scope_id` | No (derived per-call) | New scope_id created per manager invocation | OK |
| `owner_id` | Yes (identity) | Yes (verbatim from parent) | OK |
| `actor_id` | Yes (identity) | Yes (verbatim) | OK |
| `surface` | Yes (transport context) | Yes (verbatim) | OK |
| `room_id` | Yes | Yes (verbatim) | OK |
| `room_context_id` | Yes | Yes (verbatim) | OK |
| `visibility` | Yes (carries with identity) | Yes (verbatim) | OK |
| `policy_id` | Yes | Yes (verbatim) | OK |
| `acting_as` | **Yes** (principal identity carries down) | Yes (verbatim) | OK |
| `reply_to` | **Yes** (surface routing carries down — see line 142-145 comment in pydantic_classes) | Yes (verbatim) | OK |
| `history.*` | Yes (history shape is a session concern) | Yes (verbatim) | OK |
| `resources.*` | **Partial** — caller can broaden via intersection? Today intersects per manager scope_contract.resources. | Yes (intersected) | Possibly correct — narrowing makes sense for resource access |
| **`tools.allowed_tools`** | **NO** — tools are local to each manager; should not propagate at all. (See section 7.) | REPLACE iff child declares scope_contract.tools.allowed_tools, otherwise INHERIT verbatim | **Bug class: wrong axis of constraint propagation. Tools shouldn't flow; authority should.** |
| `tools.blocked_tools` | Yes (defense-in-depth union: parent blocks + child blocks) | Yes (union) | OK |
| `tools.requires_approval_tools` | Yes (union) | Yes (union) | OK |
| `tools.allow_external_side_effects` | Narrowing (child=false wins) | Yes | OK |
| `entities.*` | Narrowing (intersected) | Yes | OK |
| `cards.*` | Narrowing | Yes (caps inherited) | OK |
| `writes.*` | Narrowing (child=false wins) | Yes | OK |
| `delivery.*` | Inherit | Yes (verbatim) | OK |
| `approval.authority_level` | Yes (identity-shaped — sub-manager doesn't elevate above parent) | Yes | OK |
| `retention.*` | Inherit | Yes | OK |
| `execution.*` | Inherit (caps) | Yes | OK |
| `delegation.*` | (empty placeholder) | N/A | N/A |
| `skills.always_inject` | Yes (per comment in code: "propagates through nested agent calls because ScopeContext does") | Yes (verbatim) | OK |
| `skills.denied_skills` | Yes (union or inherit) | Yes (verbatim) | OK |

**Only one field is broken: `tools.allowed_tools`. The architectural intent is documented and correct; the implementation has a config-shape gap that silently bypasses the fix.**

---

## 6. Bug class summary

There are two ways to characterize the bug, both valid:

### Code-level characterization (narrow)

`_apply_manager_narrowing` at `scope_adapter.py:435` only triggers the REPLACE path when `manager_config['scope_contract']['tools']['allowed_tools']` is present. When the child manager's `scope_contract.tools` omits the `allowed_tools` key (declares only `blocked_tools` or omits the block entirely), the parent's narrowed allowed_tools is inherited unchanged. This is silent — no warning, no log line saying "scope_contract.tools.allowed_tools missing, inheriting parent's narrowing."

### Architectural characterization (wide)

The fact that two config fields (`allowed_tools` at the manager top level + `scope_contract.tools.allowed_tools`) must both be kept in sync for correct behavior is itself the bug class. The same information lives in two places with different purposes:
- Top-level `allowed_tools`: what the manager's own planner sees in its prompt
- `scope_contract.tools.allowed_tools`: what the manager presents to parents who delegate to it

The May 5 fix's intent — "delegating to a manager implicitly allows the manager's internal tools" — was correct, but it left the implementation requiring manual mirroring. Any new manager added today that follows the http_manager pattern (declare allowed_tools at top level, declare only blocked_tools under scope_contract) will silently break the same way.

### Reproducibility

Direct delegation (room_manager → sub_manager) works because room_manager's scope is broad enough. Two-hop delegation through any narrow-scope intermediate (emi_team_manager, in our case) breaks for sub-managers that don't mirror their allowed_tools. So tests that exercise only single-hop paths give false confidence.

---

## 7. Proposed architectural shift — authority caps, not tool intersection

Earlier drafts of this section listed three "shapes" (A/B/C) all of which kept the tool-list-intersection model and only varied how the lists were constructed. After discussion the user named the actual flaw: **the constraint that propagates downward through delegation should be `authority` (a scalar), not `allowed_tools` (a list).** Tools are local choices each manager makes within its authority bound; tools don't flow.

### The corrected model

```
What propagates downward:
  authority_level (scalar)    cap rule: child = min(child.declared, parent.authority)
  blocked_tools (list)        safety floor; union of parent ∪ child denylists
  identity fields             owner_id, actor_id, acting_as, room_id, reply_to, ...

What does NOT propagate:
  allowed_tools (list)        LOCAL to each manager — each planner reads its own config
```

### Three orthogonal primitives: allowed, visible, authority-cleared

These are NOT the same thing. Today the code conflates them in places (notably `tool_visibility.always_show` doubles as both a rendering hint and a security bypass). The clean separation:

```
For a planner running in scope S, inside manager M:

  allowed = M.config.allowed_tools  -  S.blocked_tools
                  (capability — what could be invoked at all)

  visible = subset of `allowed`, chosen by:
              - M.config.tool_visibility.always_show     (forced into prompt)
              - M.config.tool_visibility.ranker          (heuristic / LLM narrower)
              - agent-promoted via find_tool / discover_skills
                  (knowledge — what's in the prompt this turn)

  authority_cleared = tool.approval_min_authority <= S.authority_level
                  (use-time gate; filters at invocation, not rendering)
```

**Implications:**

- A tool can be **allowed but not visible**: the planner doesn't see it in its prompt, but if it discovers the tool via `find_tool` / `discover_skills`, it can use it. This handles the large tail of MCP tools and niche utilities — saves prompt tokens and improves LLM decision quality.
- A tool can be **visible but not authority-cleared**: shown with a `[requires approval]` tag. Planner may pick it; the invocation routes through the approval gate rather than executing immediately.
- `find_tool` and `discover_skills` are first-class promoters from "allowed-but-hidden" → "visible." They don't grant new permissions; they reveal existing capability. Today this is half-implemented; in the new model it's the canonical handle for the large-catalog problem.
- `tool_visibility.always_show` becomes a pure rendering hint. It stops being a security bypass (which it half-was today and led to confusion in tool_scope_service).

Each tool carries `approval_min_authority: int` in its `tool_contract.json` metadata (some already do — `bash_manager` has 99). At the planner's tool-render step, the visible list is filtered by `tool.approval_min_authority <= scope.authority_level`. Tools above the agent's authority are hidden, blocked, or routed through approval.

### How this resolves today's bug

`emi_team::planner` sees `[http_manager, web_manager, ...]` and `http_request` is NOT in its list — not because anyone constrains it, but because emi_team's own config doesn't list it. (That's `feedback-emi-team-no-owned-leaves` expressed naturally as a consequence, not a contract.)

When emi_team picks `http_manager`, the delegation creates a child scope with `authority = min(http_manager_config_authority, emi_team_authority)`. `http_manager`'s planner sees ITS OWN `allowed_tools` (http_request, oauth_token_refresh, ...) from ITS OWN config. The parent's tool list never touched it. The child's planner couldn't construct a more-privileged scope than the parent because `authority` is capped, but the tool LIST is local.

If emi_team had `authority=80` and `http_request` had `approval_min_authority=60`, http_request appears. If emi_team had `authority=50`, http_request would be hidden or require approval. Same gate, but expressed as a numeric comparison, not a list-membership check.

### Why this is structurally better

1. **No dual source of truth.** A manager's `allowed_tools` is one config field, period. Parents never duplicate or mirror it.
2. **Adding a new manager is mechanical.** Declare your tools at top-level, set your authority floor. No `scope_contract.tools.allowed_tools` field to remember to mirror, no inheritance gotchas, no silent-strip bugs.
3. **The "no-owned-leaves" rule becomes a consequence, not a contract.** emi_team doesn't list http_request in its tools because http_request isn't one of emi_team's tools. End of question.
4. **Authority is auditable.** "Can this scope do X?" is a single comparison (`X.required_authority <= scope.authority`), not a graph walk through intersection trees.
5. **Defense-in-depth still works via `blocked_tools`.** A Slack room can hard-deny `install_tool` regardless of authority. Per-room policy doesn't get tangled up with per-manager scopes.
6. **Matches what other permission systems converged on.** Unix UID/GID + permission bits, OAuth scopes vs token, AWS IAM principal+action. Sets-intersected-down-the-chain has been tried and abandoned often enough that there's a reason.

### Implementation implications

- `_intersect_allowed_tools` in scope_adapter.py becomes dead code. So does most of `_apply_manager_narrowing`'s `tools_cfg` handling.
- Every tool needs `approval_min_authority` set on its `tool_contract.json` metadata. Audit needed — most tools don't have it today.
- `tool_visibility.always_show` becomes a per-manager UI rendering hint about which tools to put at the top of the planner's tool list. It stops being a security mechanism (which it half-was today).
- Manager configs lose `scope_contract.tools.allowed_tools` entirely. They keep `scope_contract.tools.blocked_tools` (still load-bearing as a safety-floor denylist).
- `_apply_manager_narrowing`'s `allowed_tools` branch is removed; the `blocked_tools` union behavior stays; the `authority_level` capping is the new (or extended) branch.
- The visible-vs-allowed distinction is made explicit at the rendering boundary: the planner's tool prompt is built from `visible_tools`, which is a *rendering* subset of `allowed_tools`. Today these are conflated; in the new model, allowed is the security boundary and visible is a UX/token-budget choice. `find_tool` and `discover_skills` promote allowed-but-hidden tools to visible without changing the allowed set.

### Conceptual rotation

Today the model is **"permissions are sets you intersect down the chain."** The corrected model is **"authority is a scalar you cap down the chain, and tools are local choices each manager makes within its authority bound."**

The first model treats `allowed_tools` as a security constraint that must be transitively enforced. The second model treats `allowed_tools` as a manager's own description of what it does — orthogonal to security, which is the authority axis.

---

## 8. Open questions

These need answers before any code change ships:

1. **Why isn't `always_show` saving http_request?** http_manager declares `tool_visibility.always_show: [http_request, ...]`. The filter in `tool_scope_service.py:415-420` bypasses the scope-contract filter for items in always_show. Either the always_show isn't being passed through correctly, or there's another filter downstream stripping it. Need to instrument and verify before committing to a shape — if always_show is supposed to save the day here but doesn't, there's a second bug to fix.

2. **Does any current code depend on `scope_contract.tools.allowed_tools` mirroring `allowed_tools`?** If Shape B is chosen, need to confirm no consumer reads from `scope_contract.tools.allowed_tools` expecting a different list than top-level `allowed_tools`.

3. **What does Shape B do about `blocked_tools` and `requires_approval_tools`?** Those still need to live somewhere parents can see them. Probably stay in `scope_contract.tools` as the "what the parent should know about this child" surface. Need to think through if removing `allowed_tools` from that block makes the remaining contract field coherent.

4. **For Tier 2/3 scope construction (routines, courier scopes), should they be affected?** Those don't go through `_apply_manager_narrowing` because they're not invoked as sub-managers. They construct their own ScopeContext from scratch. Shape A/B/C wouldn't change them.

5. **Subconscious scope inheritance pattern** — when a routine spawns a sub-manager, does it use the same path? Need to spot-check `subconscious/run_*.py` to confirm they pass `scope_context` to manager invocations or rely on system-scope derivation.

---

## 9. Companion memories

- `feedback-scope-is-locus-of-mode-control` — "ScopeContext is THE surface for any mode dimension."
- `feedback-scope-context-dict-or-object` — "Blackboard stores it as model_dump'd dict; agent Messages carry the object."
- `feedback-submanager-inherits-parent-scope` (just written) — the symptom captured.
- `feedback-emi-team-no-owned-leaves` — emi_team correctly excludes manager-internal tools from its own surface. The exclusion is the right model; the bug is propagation.
- `feedback-curated-allowed-tools` — narrower lists improve planner quality.
- `project-http-sandbox-managers` — context for the http_manager/sandbox_manager wrapper pattern.

---

## 10. Investigation deliverables (this audit)

- This document (`docs/architecture/SCOPE_AUDIT.md`) — read-only, no code touched.
- `scratch/http_team_planner_turn3_reconstruction.txt` — earlier reconstruction of the failing planner's prompt, kept for future debugging reference (gitignored).
- `feedback_submanager_inherits_parent_scope.md` (memory) — captures the user-facing symptom for future sessions.

No code changes proposed yet. The decision on which shape (A / B / C) to pursue is the next step, and should happen after open question (1) is answered.
