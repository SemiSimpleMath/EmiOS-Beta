> **⚠️ ARCHIVED / HISTORICAL — DO NOT EXECUTE AS WRITTEN (2026-06-17).** The unified-scope work **shipped via the transitional *overlay* path, NOT the clean cutover** this spec describes. The §5 "DELETE" targets — `_overlay_scope_yaml_permission`, `build_system_scope_context`, `_derive_*_scope`, `SCOPE_CONTRACT_STRICT` — are **live and load-bearing**; deleting them would break scope. For current behavior see **[SCOPE.md](SCOPE.md)**. Kept as the historical plan.

---

# Scope Refactor — Clean Cutover Spec

**Status:** spec / not yet executed. Authored 2026-05-30.
**Supersedes the *plan* sections of** `SCOPE_AUDIT.md` (which remains the
investigation record). This is the end-state design + teardown order for the
unified-scope refactor, written for a **clean cutover** (the assistant offline during the
work) rather than the live, shimmed rollout the earlier steps used.

Companion memory: `project_unified_scope_design`, `feedback_scope_is_locus_of_mode_control`,
`project_courier_scope_and_pod_indirection`, `feedback_no_circular_characterization_tests`.

---

## 1. The principle

**One method builds scope, everywhere, unless a site has a defensible reason to
differ.** A scope-building site is one of exactly three things:

1. **`load_scope(source, identity) -> ScopeContext`** — the single builder. Reads
   a source's `scope.yaml` (permission), stamps per-request identity, applies the
   fail-closed floor, returns a fully-validated `ScopeContext`.
2. **Inheritance** — a sub-manager receives the parent scope verbatim
   (`for_sub_manager`). Propagation, not construction. Stays.
3. **Defensible-differ** — `for_courier_call`: a band-100 capability token for
   deterministic secret-unsealing code, which has no source declaration and must
   cross the 99/100 cap that blocks all LLM agents. Stays, out of `load_scope`.

Everything else collapses onto (1).

## 2. The three buckets (what scope.yaml may contain)

`ScopeContext` is a kitchen sink; its sub-policies fall into three disjoint
buckets. This split is the backbone of the whole design.

| Bucket | Sub-policies | Source of truth |
|---|---|---|
| **Permission** | `tools` (incl. `per_manager`), `pods`, `resources`, `entities`, `cards`, `writes`, `approval`, and `delivery.{auto_send, allow_initiation}` | **`scope.yaml`** (the ONLY thing a source declares) |
| **Room/session behavior** | `history`, `retention`, `execution` | room/source config + builder defaults |
| **Identity (request-shaped)** | `scope_id`, `owner_id`, `actor_id`, `surface`, `room_id`, `room_context_id`, `visibility`, `policy_id`, `reply_to`, `acting_as`, `delivery.allowed_reply_types`, `skills.always_inject` | **stamped at load time** from the request envelope; a file may NOT author these |

Key consequences:
- **`acting_as` is identity, not permission.** It is stamped per-request (from
  `/actas`), never in `scope.yaml`. Skills derive from it at injection time
  (`context_injector`), and the permission overlay deliberately never touches the
  `skills` block. So principal-mode and static scope are orthogonal by
  construction — no conflict.
- **Fail-closed floor:** a source that declares no `tools.allowed_tools` gets
  `[]` (no tools), never the permissive `["all"]`. Grants exist only where
  written.

## 3. Authority = the single trust axis

There is ONE trust scalar: `scope.approval.authority_level` (0–100). Pods reuse
it directly (`pod.min_authority`). Bands: 10 public / 50 chat / 70 gated chat /
**99 user-equivalent (max any LLM agent reaches)** / **100 courier-only**.

The **99/100 cap is a hard safety boundary**: no LLM agent — not even
master_room at 99 — can read a band-100 projection (full SSN, full account
number, raw bytes). Only deterministic non-LLM substitution code runs at 100,
via `for_courier_call`. Authority caps **monotonically down** the call tree: a
sub-manager's authority is `min(its declared cap, parent's effective)`, never
higher.

Pods are **pointers, never secret bytes**: a projection row holds an env-var
name (`env_ref`) or a file path (`file_ref`); the deterministic resolver reads
`os.environ[env_ref]` / the file at substitution time. The raw secret never
enters DB-stored pod data, an LLM prompt, or agent context — the agent only ever
handles the opaque `datapod:kind:id/projection` ref string.

## 4. End-state: how a scope gets built

```
request (room inbound / routine tick / pipeline run / sub-manager call)
   │
   ├─ sub-manager call?  → inherit parent scope verbatim (for_sub_manager). DONE.
   │
   ├─ courier unseal?    → for_courier_call mints a band-100 capability. DONE.
   │
   └─ otherwise          → load_scope(source, identity)
                              source   = the declaring entity's scope.yaml
                                         (room / routine / pipeline / job)
                              identity = stamped from the request envelope
                              → fully-validated ScopeContext (fail-closed)
```

`load_scope` owns all three buckets at build time:
- **Permission** ← the `scope.yaml` file.
- **Room-behavior** ← folded-in defaults (today's builder logic moves into the
  loader / a small `room_behavior_defaults()` helper).
- **Identity** ← the `identity` envelope.

No overlay. No legacy dict to patch. No "no-file → fall back to ROOM.md."

## 5. What gets DELETED in the clean cutover

These exist ONLY to support live, room-by-room migration. With the assistant offline they
are pure cruft:

| Delete | File | Why it was there |
|---|---|---|
| `_overlay_scope_yaml_permission` | `room_scope_builder.py` | the transition shim that layered scope.yaml permission onto a legacy ROOM.md-derived dict |
| ROOM.md `permissions:` frontmatter reading | `room_resource_loader.py` + builders | **confirmed dead fields** — `allowed_tools`/`blocked_tools`/`per_manager` in frontmatter were never read by the builder (it read `request_data` instead). Actively misleading (e.g. the Justin Slack channel's ~120-tool blocklist was never enforced). |
| `_derive_room_scope`'s bespoke build | `scope_adapter.py` | already collapsed onto the reference builder (commit `034f2da3`); becomes a thin `load_scope` call |
| `_derive_system_scope` | `scope_adapter.py` | hand-rolled system scope from `message.data`; replaced by `load_scope` |
| `build_system_scope_context` permissive `[all]` defaults | `scope_adapter.py` | fail-OPEN system default; replaced by fail-closed `load_scope` |
| `build_system_scope_for_room` | `system_scope_builder.py` | dayflow's parallel room builder that skipped overlay/per_manager/pods/skills |
| `build_pipeline_scope_context` + the fixed section-copy allowlist | `pipelines/scope_policy.py` | silently dropped `pods`/`skills`; based on the `[all]` default |
| `SCOPE_CONTRACT_STRICT` flag + non-strict branch | `scope_adapter.py` | always strict / fail-closed now; the flag was a live-migration safety valve |
| `for_system_routine` / `for_internal_invocation` (as separate builders) | `scope_adapter.py` | become thin wrappers over `load_scope`, or are inlined |

## 6. What MOVES (not deleted — it lives somewhere even in the clean version)

- **Room-behavior assembly** (history/retention/execution defaults) → into
  `load_scope` (or a `room_behavior_defaults()` helper it calls). Today it's
  duplicated across `build_scope_contract_for_room_request`, `_derive_room_scope`,
  `build_system_scope_for_room`. One copy after.
- **Identity stamping** → already in `load_scope`. The various builders' identity
  derivation (actor_id chains, surface resolution) converges here.
- **The principal→skills map** → today hardcoded in `_PRINCIPAL_SKILL_PACKS`
  (room_scope_builder) AND in per-skill registry metadata. Collapse to the
  registry as the single source (separate task; see TODO #1).
- **The `scope.yaml` files** → unchanged. This session's room migrations are the
  end-state artifact; only the transition glue around them is thrown away.

## 7. What KEEPS differing (and why)

- **`for_sub_manager`** — returns `parent_scope` verbatim. Pure inheritance;
  collapsing it would make a sub-manager rebuild from a source file instead of
  inheriting the parent's already-narrowed surface, re-granting restricted
  tools/pods and breaking fail-closed propagation.
- **`for_courier_call`** — band-100 capability token for deterministic secret
  unsealing. No source declaration; per-call authority (50 for an expiry read,
  100 for the unseal); must stay decoupled from any room overlay so a secret read
  can't pull an unrelated room's scope. (Cosmetic follow-up: rename to a
  `CourierCapability` type so it doesn't look like agent re-auth — TODO #9.)

## 8. The hard requirement: every source must have a scope.yaml BEFORE its builder is deleted

This is the one rule the clean approach cannot skip. `load_scope` is
fail-closed: a source with no `scope.yaml` resolves to **no tools**. So deleting
a legacy builder before its sources declare scope = those flows silently do
nothing on next boot.

**Status of sources:**
- **Rooms** ✅ — migrated this session (master_room, templates, 4 UI rooms, sms,
  + local personal rooms). Ready.
- **Routines** ❌ — ~44 in `configs/routines/public/`. None declare scope; they
  run on `build_system_scope_context`'s `[all]` default today. MUST author scope
  for each (or a shared routine default) before deleting that builder.
- **Pipelines** ❌ — daily_insights, dayflow, dj, entity_cards_v2,
  kg_maintenance_pipeline, kg_pipeline, weekly_insights. Some have a legacy
  `scope.json` to convert; others have nothing.
- **dayflow_orchestrator** ❌ — uses `build_system_scope_for_room`; its ROOM.md
  scope is dead today.
- **`invoke_agent` internal calls** ❌ — `for_internal_invocation`.

**Authoring scope for non-room sources is a JUDGMENT step, not mechanical** —
same "what should this actually be allowed to do" decisions made per-room. Most
routines/pipelines are autonomous system work that legitimately needs broad
access, but "broad" must be *declared deliberately*, not inherited from a
fail-open default. Each gets reviewed with the user. Do NOT blanket-default them
to `[all]` "to preserve behavior" — that re-creates the exact fail-open the
refactor removes.

## 9. Teardown order (clean cutover, the assistant offline)

Each step ends test-green. Intermediate states may be non-runnable (nothing is
live) — that's the point of doing it offline — but we still go in verifiable
chunks because (a) it's core permission code and (b) the incremental net has
caught multiple errors this session.

**Phase A — make `load_scope` the one room builder (rooms are ready):**
1. Fold room-behavior + identity assembly from
   `build_scope_contract_for_room_request` into `load_scope` (so the file isn't
   the only input — the loader produces a complete ScopeContext directly).
2. Point room ingress at `load_scope`; delete `_overlay_scope_yaml_permission`
   and the ROOM.md permission-frontmatter reading.
3. `_derive_room_scope` → thin `load_scope` call (already half-done).
4. Verify: room scope suite + ingress parity, all green.

**Phase B — source resolution + non-room declarations:**
5. Add `load_scope_for_source(kind, id, identity)` resolving
   routine/pipeline/job → its `scope.yaml` path.
6. **Author scope.yaml for every routine, pipeline, job, dayflow** (the judgment
   step, §8) — with the user, source by source.
7. Fix broken `dj_manager/scope.py` (TODO #6).

**Phase C — delete the legacy system builders:**
8. `build_system_scope_for_room` (dayflow), `_derive_system_scope`,
   `build_system_scope_context` `[all]` defaults, `build_pipeline_scope_context`,
   `for_system_routine`, `for_internal_invocation` → `load_scope` /
   `load_scope_for_source`. Delete `SCOPE_CONTRACT_STRICT` and the non-strict
   branch.
9. Verify: full suite + boot the assistant once, watch it come up clean.

**Phase D — expand per_manager to 5 sub-blocks (deferred, optional):**
10. `ScopeToolRule` grows `{authority, resources, entities}` beyond `{allow,
    block}` so `per_manager` can express everything the old manager
    `scope_contract` did. Only needed if a source wants per-manager
    authority/resource narrowing; not required for the cutover.

## 10. Verification strategy

- **Non-circular tests only.** A migrated source's scope is asserted against
  **hardcoded golden constants**, never builder-vs-loader (the overlay made that
  tautological — see `feedback_no_circular_characterization_tests`). Every
  permission assertion must be one that FAILS if the scope.yaml drifts — prove it
  with a mutation (flip a value, see the test go red, revert).
- **Fail-closed enforcement** asserted at `check_tool_access` (execution gate),
  not just visibility — `[] → tool denied`.
- **Boot test** — the final gate is starting the assistant and confirming each surface
  (chat, dayflow tick, a routine, a pipeline) builds a sane scope and runs.

## 11. Bugs to fix in-flight (found by the audit, independent of the cutover)

- ~~**Fail-open visibility seam:** `tool_scope_service._apply_scope_filters` treats
  empty `allowed_tools` as falsy → skips the visibility filter (planner shown
  tools it can't run).~~ **FIXED 2026-05-31.** `if allow_set and "all" not in allow_set`
  → `if "all" not in allow_set` (empty → show nothing). Sibling fail-open in
  `tool_policy_resolver.get_visible_tools` (empty `visible_raw ∩ allowed` fell back
  to full `allowed`) fixed in the same pass — now returns the strict intersection.
  Both align visibility with the execution gate. (was TODO #5)
- **`dj_manager/scope.py`** has a syntax error + bad kwarg; can't import. (TODO #6)
- **Broad path-guardrail test** is red on main (~15 pre-existing `parents[N]`
  offenders) — unrelated tech debt. (TODO #10)

## 12. Out of scope for this refactor

- Slimming the `ScopeContext` pydantic model (removing history/retention from the
  struct) — separate, larger, touches every `scope.history` consumer.
- The courier capability rename (cosmetic — TODO #9).
- The principal→skills source unification (TODO #1) — adjacent but independent.
