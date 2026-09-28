# Modular EmiOS — making features installable

Status: plan, written 2026-09-17 from a read-only survey of the repo. No code.
These are large refactors; nothing here starts until explicitly scheduled.
Companion to `emi_self_extension.md` (the assistant adding features to itself)
and `coding_agent_for_emi.md`. A module contract is what lets either a human
contributor or a self-coder add a feature without touching core.

Line numbers cited below are as of 2026-09-17 and will drift.

---

## 1. Goal

A person says "I would like EmiOS to do X." Someone (a contributor, the
owner, or the assistant's own coder) builds X as **one self-contained
module**: a directory that can be added, removed, and shared without editing
shared files. The core loads it the same way in a dev checkout and in a
packaged install.

Non-goals for this plan:

- A public marketplace or remote install. The first target is "a directory
  you drop in," not a registry server.
- Changing how agents, managers, or the dayflow decide anything. This is
  plumbing, not cognition.
- Backwards compatibility layers. When a hook replaces a hand edit, every
  existing caller moves to the hook in the same change.

---

## 2. Where we are today (measured)

### 2.1 What already works drop-in

Seven filesystem registries exist. Drop a directory in, restart, it is loaded:

| Kind | Registry | Notes |
|---|---|---|
| Agents | `app/assistant/agent_registry/agent_registry.py` rglob for `config.yaml` | missing `system.j2`/`user.j2` raises at boot |
| Tools | `app/assistant/lib/tool_registry/tool_registry.py:285-392` | broken `tool_contract.json` refuses boot (correct) |
| Control nodes | `agent_registry.py:395-434` glob of `control_nodes/*.py` | filename to class-name convention is load-bearing |
| Managers | `app/assistant/manager_registry/manager_registry.py` | accepts **`config.yaml` only** |
| Orchestrators | `app/assistant/orchestrator_registry/` | undocumented kind |
| Skills | `app/skill_registry/skill_registry.py` walks `skills/` + `skills/private/` | malformed skill logged, not fatal |
| Resources | `app/resource_manager/resource_manager.py` rglob | existing file hot-reloads on mtime; new file needs restart |
| Routines | `configs/routines/{public,private}/*.json` + `@routine_handler` | **hot**, no restart; private overrides public by id |
| Materializers | `app/assistant/pod_store/materializers/__init__.py:125-140` pkgutil auto-import | cleanest hook in the repo |
| Rooms | `room_resource_loader.py` reads `ROOM.md` per message | **hot**; no registry at all |

Caveat: loaded is not reachable. An agent or tool becomes usable only when a
manager or agent `config.yaml` names it under `agents:` / `state_map` /
`allowed_tools`. That file is the real hub, and it is a boot gate (an unknown
agent name raises in `app/assistant/validation/agent_validator.py:265`).

### 2.2 Layers with no registration hook

Every feature that touches one of these edits shared Python or a shared
template by hand:

| Layer | Where the hand edit lives today |
|---|---|
| Flask blueprints | `app/create_app.py:173-336` (~70 `register_blueprint` calls), `app/routes/__init__.py` |
| Nav entries | `app/templates/_top_bar_dashboards_menu.html`, `_top_bar_dev_menu.html`, `_my_life_menu.html` (duplicated across two shells) |
| Socket events | `app/socket_handlers.py`: one `register_socket_handlers(socketio)` function; DJ has 5 handlers inline at L250-410, one with 30 lines of business logic |
| Service lifecycle | `app/bootstrap.py:339-347` (start) and `:357-402` (LIFO shutdown, enumerated by name) |
| DB tables | `app/database/table_initializer.py:46-81` **and** `setup.py:166-180` (same six DJ tables twice) |
| Transports | `room_session_manager.py:114-117` constructor attributes; `services/surfaces/factory.py:15-27` hardcoded dict; `initialize_system.py:95-101` ticket adapter list |
| Pipelines | `app/assistant/pipelines/pipeline_registry.py:48-78` literal list (plus a vestigial second resolver in `pipelines/registry.py`) |
| LLM providers | `app/configs/llm_classes_dict.py` (3 dicts), `app/services/llm_client.py`, `configs/model_tiers.yaml`, `configs/llm_prices.json` |
| MCP trust | `app/assistant/lib/tool_registry/mcp_trust_policy.py` hardcoded list |
| Governed vocabularies | `configs/pod_kinds.json`, `configs/belief_tags.yaml`, `configs/belief_domains.yaml`, `configs/pod_tags.yaml`, edited **in place** inside shared entries |
| Env / account descriptors | `.env.example`, `app/assistant/env_registry/builtins.json`, `resources/resource_emi_accounts.json` (same bot account described twice), `app/routes/preferences.py` env groups |
| Subsystem flags | `configs/subsystems.yaml` + `_SUBSYSTEM_LABELS` in `app/routes/subsystem_route.py`; unknown flag defaults **on** |

### 2.3 What a feature costs

Three real features traced end to end:

| Feature | Top-level dirs touched | Shared files edited | Worst coupling |
|---|---|---|---|
| DJ / music | 6 | ~15 | socket handlers inline; tables declared twice; named in bootstrap + shutdown |
| Meal planning | 9 | ~20 | 19 runtime modules loose in shared `app/assistant/subconscious/`; its variants are words inside shared `pod_kinds.json` entries |
| Telegram | 4 | ~30 | transport is a constructor attribute; `approval_gateway.py:462` imports the bot directly; `switchboard/prompts/system.j2:47` names it in an LLM prompt |

Meal is the best evidence the design works: its 11 agent dirs, 2 managers,
5 routines and 7 resources needed **zero** central edits. Everything it did
have to edit is in the 2.2 table.

### 2.4 Packaging: code tree vs data dir

`app/assistant/utils/path_utils.py` is a clean contract: `get_repo_root()`
is the read-only code layer, `get_data_dir()` (`EMI_DATA_DIR`) is writable
state. In dev they coincide. In Docker (`EMI_DATA_DIR=/data`) and in the
planned desktop package they do not.

Only routines, windows, routine scope sidecars and `smart_home_tools.json`
load from the data dir. Agents, tools, managers, rooms, skills, pod kinds,
cameras and MCP registries all load from the **code tree**. So in any
packaged install a user cannot add a module at all. `EXTENDING.md:50-53`
says cameras and pod kinds are hot-loaded; under `EMI_DATA_DIR` they are not
reachable.

There is no plugin, extension or addon concept anywhere. The nearest
primitive is `install_mcp_tool` (`mcp_installer.py:25-93`): registers a
capability at runtime without restart, but writes its registry into the code
tree. The public/private splits that exist (`configs/routines/private`,
`skills/private`, `.example` seeds, `_personal.md` overlays) are
leak-prevention for the owner's data, not a channel for sharing.

### 2.5 Contributor reality

One outside contributor, 24 commits, 18 logged PRs (`contrib_log/`). Every
PR was providers, proxy, paths, Docker, migrations, settings UI. **None used
an extension point.** The friction an outsider hits first is the deployment
seam, not the agent architecture the docs describe.

There is no `CONTRIBUTING.md`, no `.github/`, no CI. Review is fully manual
per the standing posture. `README.md` is install-only. The rules that get a
PR rolled back live in `CLAUDE.md`, addressed to Claude Code, not to humans.
The pre-commit PII hook fails open on a fresh clone (`pre-commit:36-38`), so
it protects the maintainer's machine, not the contribution.

Tests: tools have a real double-based harness (`tool_tests/_helpers.py`).
Rooms have a DB-sandboxed harness (`room_test_harness/`) but LLM calls,
Chroma and outbound side-effect tools are live. Agents have no harness.

---

## 3. Target design

### 3.1 What a module is

```
modules/<name>/
  module.yaml                 # the manifest (3.2)
  agents/<ns>/<agent>/        # same shape as app/assistant/agents/
  tools/<tool>/               # same shape as app/assistant/lib/tools/
  managers/<mgr>/config.yaml
  control_nodes/*.py
  rooms/<room>/{ROOM.md,scope.yaml}
  routines/*.json
  routine_handlers/*.py
  resources/**                # same layout as resources/
  skills/<skill>/SKILL.md
  materializers/*.py
  routes/*.py                 # Flask blueprints
  socket_handlers/*.py
  templates/**  static/**
  models/*.py  migrations/*.py
  vocab/{pod_kinds,belief_tags,belief_domains,pod_tags}.yaml
  tests/**
  README.md
```

Every subdirectory is optional. The sub-layouts are **identical** to the
existing core layouts so the existing loaders need a second root, not a new
format. Core itself stays where it is; a module is an overlay.

### 3.2 The manifest

`module.yaml` declares the things that today need a hand edit. Draft fields:

```yaml
name: dj
version: 0.1.0
description: Music selection and playback
requires_core: ">=0.3"
subsystem_flag: dj_manager          # replaces configs/subsystems.yaml entry + label

services:                           # replaces bootstrap start/stop wiring
  - import: modules.dj.service:DJManager
    di_name: dj_manager

blueprints:                         # replaces create_app.py edits
  - import: modules.dj.routes.music:music_bp
    url_prefix: /music

nav:                                # replaces menu partial edits
  - menu: dashboards
    label: Music
    href: /music
    when_flag: dj_manager

socket_events: auto                 # discovered from socket_handlers/ via decorator

tables: auto                        # discovered from models/ ; migrations/ run by the runner

env:                                # replaces .env.example + preferences.py groups
  group: Music
  vars: [MUSICKIT_TEAM_ID, MUSICKIT_KEY_ID, MUSICKIT_P8_PATH]

accounts:                           # replaces builtins.json + resource_emi_accounts.json
  - id: assistant_telegram
    platform: telegram
    category: social

surface:                            # transports only; read by the security layer
  name: telegram
  default_authority: 40
  inline_approval: true

reach:                              # how the module becomes usable from chat
  add_to_allowed_tools:
    - manager: emi_team_manager
      tools: [play_music]
```

Rule: **the manifest declares, core decides.** A transport declares its
authority; `app/assistant/utils/surfaces.py` reads it and stays the single
source of truth. A module cannot grant itself anything the security layer
would not grant.

### 3.3 The loader

- Roots, in order: core (`app/assistant/...`, `configs/`, `resources/`),
  then `<repo>/modules/*`, then `<data_dir>/modules/*`. Later roots may add;
  they may not silently replace. A name collision between two roots is a
  boot error with both paths named.
- Each existing registry gains a `roots: list[Path]` instead of one base
  path. No new registry formats.
- Vocabulary files merge additively. `pod_kinds` keeps a `contributed_by`
  map so uninstall is mechanical and a leftover pod of a removed variant is
  reported, not silently orphaned.
- Manifest validation runs in `initialize_system.validate_all` alongside the
  agent validator: unknown field, missing import, or a `reach` target that
  names a nonexistent manager all refuse boot. Same posture as
  `tool_contract.json` today.
- A module directory may carry a `.disabled` marker, same as tools.
- Hot vs restart: same as today per kind. Routines and rooms inside a module
  are hot; anything else is restart. The loader logs which.

### 3.4 What stays central on purpose

- Authority, approval, scope resolution (`app/assistant/scope/`,
  `tool_access_control.py`). Modules declare, never enforce.
- The agent/manager validators and the fail-loud boot barrier.
- The DB write coordinator (`db_manager`). A module's tables go through it.
- `{{ assistant_name }}` injection and the no-hardcoded-name rule.

### 3.5 Trust model (decision needed, see section 6)

Two shapes, not mutually exclusive:

- **Config-and-prompt modules**: agents, managers, rooms, routines, skills,
  resources, vocab. No Python. Safe to accept from anyone; the worst case is
  a bad prompt.
- **Code modules**: tools, routes, socket handlers, services, models. These
  execute in-process with full access. They need the same posture as MCP
  servers today (`mcp_trust_policy.py`): an explicit allowlist the owner
  edits, and the loader refuses a code module not on it.

---

## 4. Phases

Each phase is independently shippable and leaves the repo working. Order
matters: 1 before 2 (hooks before the loader has anything to load), 3 after 2
(extraction proves the loader), 4 last (security-sensitive).

### Phase 0 — survey defects and doc corrections

Small, needed regardless, and they remove false claims a module author would
trip over.

| Defect | Where | Fix |
|---|---|---|
| ResourceManager bulk load scans `<code root>/resources` under `EMI_DATA_DIR` | `app/bootstrap.py:227-229` constructs `ResourceManager()` with no base; `resource_manager.py:26-28` defaults to `get_repo_root()` | pass the resources root explicitly; load both seed and live roots |
| pod kinds not data-dir reachable | `pod_kind_registry.py:32` | resolve via `get_configs_dir()` |
| cameras not data-dir reachable | `ring_analysis/camera_registry.py:35` uses `.parent` four times | resolve via `get_configs_dir()` |
| `test_deployment_path_hygiene.py:38-41` regex misses `self.base_dir / ...` and `.parent` chains | test | widen the regex; shrink the grandfathered list |
| `meal_beliefs_v2` flag read but never declared, rides default-on | `subconscious/meal_context_builder.py:320` vs `configs/subsystems.yaml` | declare it; consider making unknown flags a boot error |
| managers skill says `manager_config.yaml` | `.claude/skills/extending-emi-managers/SKILL.md:29-32`, `EXTENDING.md` table | `config.yaml`; discovery is `ManagerRegistry.preload_all` |
| rooms skill omits `scope.yaml` | `.claude/skills/extending-emi-rooms/SKILL.md` | document that `scope.yaml` replaces ROOM.md permission blocks wholesale |
| cameras skill points at deleted `access.json` and pre-split `configs/routines.json` | `.claude/skills/extending-emi-cameras/SKILL.md` | correct paths |
| rooms/routines skills say restart; both are hot | skills | correct |
| `resources/instructions/*` is blanket-gitignored (`.gitignore:320`) with 12 hand negations; the resources skill never says so | skill + `.gitignore` | document, or move shareable instructions out of the ignored path |

Verify: `pytest app/assistant/tests/non_agent_tests/test_deployment_path_hygiene.py`
plus a Docker boot with `EMI_DATA_DIR=/data` that lists loaded resources.

### Phase 1 — the missing registration hooks

Each hook copies a pattern the repo already has. Every existing consumer
moves to the hook in the same change; no dual path.

**1a. Socket events.** Pattern: `app/assistant/routine_handlers/__init__.py:32-82`.
New package `app/socket_handlers/` with `@socket_event("music_pick_request")`
and `discover_handlers(socketio)`. Move the ~12 inline handlers out of the
monolith; the DJ business logic in `music_pick_request` moves into
`dj_manager`. Verify: every event name that existed before is registered
after (a test enumerates both).

**1b. Blueprints.** `create_app.py` iterates a discovered list instead of
~70 literal calls. Core routes register via the same decorator or a
`BLUEPRINTS = [...]` in each `app/routes/*.py`. Verify: `app.url_map` before
and after is identical.

**1c. Service lifecycle.** A `Service` protocol (`start()`, `stop()`,
`di_name`). `bootstrap.py` starts discovered services in order and
`shutdown_services` stops them in reverse, replacing the enumerated LIFO
block. DJ and background_task_manager are the first two. Verify: shutdown
log shows the same stop order.

**1d. DB tables.** One collector: each `app/models/*.py` (and module
`models/`) exposes its tables; `table_initializer.initialize_all_tables()`
and `setup.py` both call the collector. Delete the duplicated six-table block
in `setup.py`. Verify: `test_fresh_db_boot.py`; schema diff of a fresh DB
before/after is empty.

**1e. Nav.** A nav manifest (core: one YAML; modules: the `nav:` block).
The three menu partials render from it, gated by `when_flag`. Removes the
`music_enabled` threading in `chat_bot.py:114-138`. Verify: rendered menus
match before/after in both UI shells.

**1f. Governed vocabularies.** `pod_kinds.json` etc. become the core file
plus `configs/<vocab>.d/*.yaml` merged at load, each contribution tagged
with its owner. Meal's variants move to `pod_kinds.d/meal.yaml`. Verify:
merged output equals today's file exactly; a duplicate variant from two
owners is a boot error.

**1g. Env and account descriptors.** One descriptor per module (`env:` /
`accounts:`) feeds `.env.example` generation, the preferences page groups,
`env_registry/builtins.json`, and the accounts resource. Verify: generated
`.env.example` matches the hand-written one.

**1h. Subsystem flags and pipelines.** Flags derive from manifests plus
core YAML; unknown flag name becomes a boot error (today it silently enables).
`pipeline_registry.py` becomes a directory walk of `pipelines/*/` with a
`PIPELINE_ID`; delete the vestigial `pipelines/registry.py`. Verify:
`/subsystems` page lists the same flags; every routine `pipeline_id` resolves.

Not in phase 1: transports (phase 4), LLM providers and MCP trust (leave as
allowlists; they are security surfaces and the contributor already fixed
their duplication).

### Phase 2 — module directory, manifest, loader

1. `modules/` at repo root and `<data_dir>/modules/`. Loader in
   `app/assistant/module_registry/` walks both, validates `module.yaml`,
   and hands each sub-root to the existing registries (agents, tools,
   managers, control nodes, rooms, routines, resources, skills,
   materializers, plus the phase-1 hooks).
2. Every registry takes `roots: list[Path]`. Name collisions across roots
   refuse boot.
3. `reach:` is applied at load: it appends to the named manager's
   `allowed_tools` / `agents` in memory and the validator checks the result.
   This is the one place a module edits a core config, and it is declared,
   logged, and visible on a `/modules` admin page.
4. Trust: `configs/module_trust.yaml` allowlist for code modules (3.5);
   config-only modules load without it.
5. Admin page `/modules`: loaded modules, their root, what they contributed,
   enable/disable (writes the `.disabled` marker), and the load log.
6. `extending-emi-modules` skill and `docs/recipes/ADD_A_MODULE.md`.

Verify: a fixture module under `app/assistant/tests/modules/fixture_hello/`
with one agent, one tool, one routine, one route, one socket event, one
table, one vocab entry. A test boots with it present and absent and asserts
every contribution appears/disappears. Same test run with `EMI_DATA_DIR`
pointing at a temp dir that holds the module.

### Phase 3 — extract DJ, then meal

**DJ first** (closest to shippable, no vocabulary entanglement, no
agent-facing tool). Move `app/assistant/dj_manager/`, `music_manager/`,
`afk_manager/music_afk_relay.py`, the two agents, `routes/music.py`,
`templates/music.html`, `static/{css,js}/music*`, the five models, the
migration, `resource_music_preferences.md`, `pipelines/dj/scope.yaml`, and
the three tests into `modules/dj/`. The manifest replaces every row in the
DJ column of 2.3. Path-hygiene tests that exempt
`dj_manager/music_dataset.py` by name get the module path.

**Meal second.** Prerequisite refactor: move the 19 loose meal modules out of
`app/assistant/subconscious/` and the four meal handlers out of
`routine_handlers/subconscious.py`. Then extract into `modules/meal/` with
`vocab/` carrying its pod-kind variants, belief tags and domain. Shared enum
memberships (`pending_question.py:45`, `feedback_service.py:29`) become
contributions. Onboarding questions, KG taxonomy seeds and dayflow stages
that read meal state stay core for now and are listed as known couplings.

Verify per extraction: the feature works with the module present; with the
module removed the app boots, no route/event/table/flag from it remains, and
the `contributed_by` report is empty for it.

### Phase 4 — transports as modules

Last, because 13 scope/authority tests name `telegram` as a security
guarantee and `approval_gateway.py` and `outbound_chat_publisher.py` reach
into the transport directly.

1. `TransportProtocol`: `handle_inbound`, `send_reply`, `append_inbound`,
   `append_outbound`, `build_ticket_adapter`. `RoomSessionManager` holds
   `self.transports: dict[str, Transport]` populated by discovery;
   `surfaces/factory.py` and the ticket adapter list in
   `initialize_system.py` go away.
2. Surface-specific persistence (`room_message_persistence_service.py:127-229`)
   and the `if surface == "telegram"` branch in `room_ingress_service.py`
   move onto the transport object.
3. `approval_gateway.py:459-466` calls `transport.send_message` instead of
   importing the bot. `outbound_chat_publisher.py:171-188` looks up
   `rsm.transports[name]`.
4. `surfaces.py` reads `default_authority` / `inline_approval` from the
   manifest **into** its central tables; the tables remain the enforcement
   point. Tests keep asserting on the literal `telegram` values; they now
   read them from a loaded fixture manifest.
5. The switchboard prompt line naming surfaces becomes a computed resource
   (`register_provider`, as `resource_accounts` already is).
6. Extract Telegram into `modules/telegram/` as the proof. Slack, SMS, UI
   follow the same protocol but may stay core.

Verify: the full pre-push guard suite plus `test_live_surface_smoke.py`;
authority for each surface is identical before/after.

### Phase 5 — contributor front door

This is what turns "people can say I'd like X" into a working loop.

1. `CONTRIBUTING.md`: setup, the module layout, fail-loud and no-hardcoded-
   name rules in human terms, what review checks, test layout.
2. `.github/`: CI running the pre-push guard suite and the module fixture
   test on every PR (the review protocol stops being 100% manual); a
   feature-request issue template whose fields map onto the manifest (what
   surface, what it needs to read, what it may do, which accounts, any new
   vocabulary); a PR template asking "is this a module, and if not, why".
3. An agent test harness under `agent_tests/` mirroring `tool_tests/_helpers.py`:
   render prompts with a fixed context, run the agent with a stubbed LLM
   client that returns a canned `agent_form`, assert on the decision. The
   room harness gets the deferred stub library for LLM and outbound so a
   module's room test costs nothing.
4. Post the existing help-wanted list (memory:
   `project_contributor_help_wanted_list`) once each item is reframed as a
   module. Several already are one: Oura ring, Spotify playback backend,
   location ingest, IMAP provider.
5. Make the pre-commit PII hook fail closed with a shipped neutral
   `forbidden_strings.txt` (product-level entries only) so contributors get
   the same guard.

---

## 5. The feature-request flow

Once phases 1-2 land, a request is answered by writing a module. The path
for each requester:

- **Outside contributor:** issue template, then module PR, then CI, then
  review. Config-only modules (agents, prompts, routines, skills) are
  low-risk and can be merged on CI green plus a prompt read. Code modules go
  through the full review protocol and the trust allowlist.
- **Owner via Claude Code:** the `extending-emi-modules` skill plus the
  fixture module as the canonical example. Today's ten `extending-emi-*`
  skills become sections of the module skill.
- **The assistant's own coder** (`emi_self_extension.md`): a module is
  exactly the Tier-1 "freely editable" shape that doc wants. `modules/` is
  the only tree the self-coder may write; core stays Tier 3. The manifest is
  what its verification harness checks ("does the route exist, is the event
  registered, does the flag show on `/subsystems`").

---

## 6. Open decisions

Both shape phase 2 and phase 4 and are the owner's call.

1. **Where do modules live?** Options: repo `modules/` only (shareable via
   git, not installable into a package); data-dir only (installable, not
   versioned with core); both with the collision rule in 3.3. The plan
   assumes both.
2. **May third-party modules ship Python?** If no, the contract is config-
   and-prompt only and the trust allowlist is unnecessary. If yes, the
   allowlist in 3.5 is mandatory and the `/modules` page must show which
   modules run code. The plan assumes yes with the allowlist.
3. **Does `reach:` stay?** It is the one sanctioned edit of a core config.
   The alternative is that a module ships its own room or manager and is
   only reachable through that, which is cleaner but means a new tool cannot
   join `emi_team` without a core PR.
4. **Model tier and provider registries:** leave as owner-edited allowlists,
   or let a module contribute a provider? The contributor's OpenCode PR
   touched 20 files; a provider module would have touched one. Security
   argues for allowlist; the plan leaves them out of phase 1 pending this.

---

## 7. Risks

- **Every phase-1 hook is a refactor of a file the running instance depends
  on.** Each must land as one commit with its verification test, and the
  live process restarted deliberately (Python does not hot-reload).
- **Reachability edits remain.** Even with modules, "make this tool
  available to the chat manager" is a core config change unless `reach:` is
  adopted. Without it the module story is "loaded but unused."
- **Vocabulary merge changes enforcement semantics.** `PodStore.put` refuses
  unregistered kinds; the merge must be complete before any consumer reads,
  or a module's pods fail to mint at boot ordering edges.
- **Transports are a security boundary.** Phase 4 must not let a manifest
  raise authority. Tests that name `telegram` are the regression net; they
  must keep passing on the literal values.
- **Private-data regime.** Modules are public by construction. The
  `.gitignore` enumerations for personal rooms, resources and routines do
  not cover `modules/`; a personal module needs `modules/private/`
  gitignored from day one, mirroring `skills/private/`.
- **Scope creep into cognition.** None of this changes what agents decide.
  If a phase starts touching prompts or state maps beyond `reach:`, stop.

---

## Appendix A — survey sources

- Registration seams: every registry under `app/assistant/*_registry/`,
  `app/bootstrap.py`, `app/create_app.py`, `app/assistant/initialize_system.py`,
  the ten `.claude/skills/extending-emi-*/SKILL.md`.
- Feature footprints: DJ, meal planning, Telegram, traced file by file.
- Contributor history: `contrib_log/poboato_2026-08-18_pr1-pr3.md`, git log,
  `README.md`, `BETA.md`, `EXTENDING.md`, `docs/welcome/WELCOME.md`,
  `scripts/git-hooks/`, `.gitignore`, `.dockerignore`, `scratch/PACKAGING.md`.
- Tests: `app/assistant/tests/test_setup.py`, `tool_tests/_helpers.py`,
  `room_test_harness/`, `sandbox_setup.py`.


### Belief source accounting follow-up — 2026-09-20

The belief store now owns atomic claim/evidence writes and source-derived counters. Weekly candidates enter the global belief pipeline as zero-weight interpretations; Dayflow consumes reconciled beliefs. Decay preserves source half-life snapshots. See [the contract and remaining work](belief_source_accounting_2026-09-20.md). No new ordinary-chat model calls or live database changes were introduced by this development verification.

Belief matching now separates discovery, source reading, relationship review, and final merge approval into standard agents. Python validates identity, source coverage, version fences, and atomic writes. Semantic equivalence remains an LLM judgment. Historical source recovery remains pending review.

Historical provenance now has an opt-in copy-only proposal service with standard discovery
and attribution agents. Complete source coverage and exact identities are validated; source-only
checks guard against circular support from predecessor wording. It is not wired into ordinary
chat or nightly execution. The historical evaluation remains incomplete after API credit
exhaustion. Selective link application, repeated-event accounting, and whole-claim synthesis
remain pending. See belief_engine/review/README.md.

The provenance source-review continuation completed after the API credit top-up: all
20,762 candidate relationships have initial assessments, and the outstanding source-only
checks are complete. Repeating continuation makes zero model calls. Five contextual
whole-belief reviews remain before any selective historical-source application.

### OAuth reliability and additional email providers — TODO (2026-09-20)

- [ ] Move the personal Google OAuth consent project(s) used by Gmail and Nest from Testing to In production, then reauthorize each integration to replace Testing-issued refresh tokens. First identify which OAuth client/project each integration actually uses. Publishing status and submitting for verification are separate steps; confirm the personal-use exception applies. Status: planned, no Cloud settings changed.
- [ ] Verify Gmail and Nest independently: successful read-only API access, automatic access-token refresh, persistence across an the assistant restart, and continued authorization beyond seven days without another consent prompt. Record dates and outcomes without secrets. Nest's personal Device Access use does not require OAuth API verification, and its documentation distinguishes OAuth token expiration from Device Access Commercial/Sandbox approval. This supports trying the same approach; success on this installation is still unverified. Investigate any shorter expiry separately.
- [ ] Once verified, add user-facing setup instructions with the exact Cloud Console steps, one-time reauthorization, expected unverified-app warning, safe troubleshooting, and the distinction between each user's own personal project and a broadly distributed shared OAuth client. Link from the existing integration/setup documentation. Do not present a configuration change alone as proof of lasting authorization.
- [ ] Design support for additional email providers and multiple accounts, starting with Proton Mail. Audit existing email ingestion and sending interfaces before proposing changes. Evaluate provider APIs and IMAP/SMTP adapters, account-specific credentials and sender identity, durable sync cursors, full source/pod provenance, and duplicate prevention without importing old mail as new actionable intake. Preserve existing outbound approval rules.
- [ ] Evaluate Proton Mail Bridge as the initial Proton integration candidate: it exposes local IMAP/SMTP and currently requires a paid Proton Mail plan. Check deployment/startup requirements, credential storage, and behavior when Bridge is offline. This is a design investigation, not a claim of current the assistant support.

References checked 2026-09-20:

- [gog setup: production publishing without verification for personal use](https://gogcli.sh/quickstart.html)
- [Google OAuth personal-use verification exception](https://support.google.com/cloud/answer/13464323?hl=en)
- [Nest Device Access authorization and refresh-token errors](https://developers.google.com/nest/device-access/reference/errors/authorization)
- [Proton Mail Bridge: local IMAP/SMTP and paid-plan requirement](https://proton.me/mail/bridge)