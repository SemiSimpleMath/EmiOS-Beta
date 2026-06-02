# Skills

## 1. What skills are

A **skill** is named, durable, structured procedural knowledge that an agent reads to perform better in a bounded domain. It tells the agent *what to do, what to look for, and what to avoid* in a particular kind of situation. It is distinct from:

- **Tools** — what the agent CAN do (capabilities)
- **Resources** — what is currently TRUE in the world (state; see §3)
- **Plans** — what to do RIGHT NOW for this specific instance
- **Memory** — what the agent learned from this run / recent runs

Skills are *operational, not declarative*. They are written as procedure (*"if you see X, click Y"*), not as facts (*"DoorDash is a delivery service"*). Examples:

- `slack-formatting` — how to render text on Slack (mrkdwn dialect, not GFM)
- `doordash-ordering` — how to navigate DoorDash modals, handle overlays, verify cart state
- `forwarding-an-email` — how to compose a forwarded message that preserves attribution

## 2. Adopted standard: agentskills.io

EmiOS adopts the **Agent Skills open standard** at <https://agentskills.io/specification>. Same format as Anthropic Claude Code, OpenAI Codex CLI, Google Gemini CLI, GitHub Copilot, Cursor, Cline, Windsurf, and OpenCode.

The decision rationale: skills are an emerging cross-vendor convention, and inventing our own format would be Betamax — incompatible with every coding agent we'd want to interoperate with. By using `SKILL.md` directly, a skill we author also serves the Claude Code subprocess that EmiCode shells out to. **One artifact, two consumers.**

### Format

Each skill is a directory at `skills/<name>/`:

```
skills/
└── slack-formatting/
    ├── SKILL.md              # required: frontmatter + body
    ├── scripts/              # optional: executable code
    ├── references/           # optional: deeper docs loaded on demand
    └── assets/               # optional: templates, data
```

`SKILL.md` is YAML frontmatter + Markdown body:

```markdown
---
name: slack-formatting                # REQUIRED. 1-64 chars, [a-z0-9-], no leading/trailing/consecutive hyphens. Must match dir.
description: Slack uses its own ...   # REQUIRED. 1-1024 chars. What + when. The match signal for discovery.
license: Apache-2.0                   # OPTIONAL
compatibility: ...                    # OPTIONAL, 1-500 chars (env requirements)
metadata:                             # OPTIONAL, free-form key/value
  author: jukka
  version: "1.0"
allowed-tools: ...                    # OPTIONAL (experimental)
---

# Body
... instructions, examples, edge cases ...
```

### Progressive disclosure

The standard expects three load tiers:

1. **Metadata** (~100 tokens) — `name` and `description`, always in context for triggering.
2. **Body** (<500 lines, ideally <5000 tokens) — loaded only when the skill activates.
3. **Resources** (`scripts/`, `references/`, `assets/`) — loaded on demand from inside the skill.

Keep `SKILL.md` short. Move detail to `references/` files the agent reads only when it needs them.

## 3. Skills vs resources — the cut

Skills and resources both end up rendered into agent prompts. They are *not* the same thing.

| Aspect | **Resources** | **Skills** |
| --- | --- | --- |
| Purpose | **State** — current truth about the world | **Instructions** — how to do something |
| Mutability | Dynamic, often updated (pipelines, UI, KG sync) | Stable, refined slowly |
| Authorship | User, setup wizard, pipelines, runtime status writers | User, skill author, eventually self-improving via Sensei |
| Selection | Static — agents declare in config | Dynamic — three injection patterns (§5) |
| Examples | `resource_user_data` (location, timezone), `resource_health_status_summary`, `resource_weather` | `slack-formatting`, `doordash-ordering`, `forwarding-an-email` |
| When wrong | Stale data, sync bug | Out-of-date instructions, refute on next run |

**Mental model**: resources are the `/etc/` of the agent — current configuration and world state. Skills are the `/usr/share/man/` — stable how-to reference the agent looks up when relevant. They share the prompt-injection delivery pipe; their lifecycles, contents, and selection rules are different.

A useful test: *"if this content needs to be updated whenever the user's situation changes, it's a resource. If this content describes a procedure that doesn't change with the user's situation, it's a skill."* `user_bio.json` (resource) updates when the user changes jobs. `slack-formatting` (skill) doesn't change because Jukka moved cities.

Resources stay where they are (`resources/` tree, see [19_RESOURCES.md](19_RESOURCES.md)). Skills get their own home at `skills/`.

## 4. Where skills live

```
skills/                      # repo root
  slack-formatting/
    SKILL.md
  ...
```

Repo-root placement is deliberate: it matches the agentskills.io convention every other tool uses, and **Claude Code (which EmiCode shells out to) reads `skills/` natively**. So a skill we author is consumed by both:

- Emi's internal agents (via the SkillRegistry described in §6)
- The local Claude Code CLI when EmiCode hands a coding task to it

No duplication, no per-agent vendor format.

## 5. Three injection patterns

Skills can reach an agent's prompt three ways. The same skill can support multiple patterns; the patterns are about *how the agent gets the skill into its context*, not properties of the skill itself.

### 5.1 Always-injected (static binding)

The agent's `config.yaml` lists the skill by name:

```yaml
name: room::chat_gate
...
skills:
  - slack-formatting
```

The context injector loads the skill body via `SkillRegistry.get("slack-formatting")` and exposes it as `{{ skills.slack_formatting }}` in the agent's Jinja prompt template.

This is the simplest pattern and matches the existing static delivery shape used by `system_context_items: [resource_X]` for resources. **Use when**: the agent always benefits from the skill regardless of context (e.g., slack-side agents always need slack-formatting).

### 5.2 Tool-discovered

The agent has `find_skill(query)` and `load_skill(name)` as tools. The planner decides:

> *"This task involves DoorDash. Let me search for relevant skills."*

`find_skill("doordash food ordering")` → registry returns matching `SkillHeader` records by description match. `load_skill("doordash-ordering")` → body lands in the next prompt's context.

**Use when**: there are many skills, only a few apply to any given task, and the agent has enough autonomy to pick. Closer to how Anthropic's published skills are typically used (the agent reads metadata, decides, fetches body).

### 5.3 Auto-injected by context

Analogous to PodInjector. A `SkillInjector` watches the message / blackboard / scope and pulls in skills whose trigger conditions match a context pattern. Examples:

- *"`room_surface == "slack"` → auto-attach `slack-formatting`"*
- *"agent is holding a pod with `kind == "email"` → auto-attach `forwarding-an-email`"*
- *"playwright manager active on doordash.com → auto-attach `doordash-ordering`"*

**Use when**: the trigger is unambiguous and you don't want every agent to declare it. Lifts the binding out of agent configs and into a centralized injection rule.

The same skill can be available via multiple paths. `slack-formatting` could be statically declared (5.1) by some agents and auto-injected (5.3) for any path that touches a slack surface. The registry doesn't care; it just answers `get(name)` and `discover(query)`.

### 5.4 Auto-injection semantics — `auto_inject_when` (the actual mechanism)

A skill opts into auto-injection (5.3) by declaring an `auto_inject_when` block in
its frontmatter `metadata`. Two trigger fields, AND-conjoined:

- **`task_keywords`** — lowercase substrings; a match against the agent's rendered
  `task + incoming_message` makes the skill *keyword-eligible*.
- **`requires_scope`** — a `{field: value}` map of conditions the live `ScopeContext`
  must satisfy. **Principle: scope is the key, the skill carries the lock.**

The governing rules (`SkillInjector`, `app/skill_registry/`):

0. **The gate is UNIVERSAL.** `requires_scope` is checked on EVERY injection path —
   static config binding (5.1), keyword auto-inject (5.2/5.3), caller-supplied
   (`skills_input`), and scope-stamped (`always_inject`). No path bypasses it. A skill
   pinned in an agent's static `skills:` list still only injects when its
   `requires_scope` matches the live scope (e.g. a persona skill statically listed by
   `emi_team::planner` injects only when `acting_as=self`, never for `user`). The
   single check is `SkillInjector.skill_gate_passes`; the resolver
   (`context_injector._resolve_skills_with_provenance`) calls it on every admission.
   A skill with no `requires_scope` passes trivially.
1. **Matching = `(keyword hit, if any) AND (every requires_scope field matches)`.**
2. **`requires_scope` fields are an explicit identity/context allowlist:**
   `acting_as`, `surface`, `room_id`, `room_context_id`, `visibility`.
   Any other field — especially the **permission bucket** (`tools`, `pods`,
   `approval`/`authority_level`, `writes`, `resources`, `entities`, `cards`) — is a
   **parse-time error (fail-loud)**. Skills gate *relevance*, never *authorization*;
   `allowed_tools` is the only grant. (See SCOPE.md: visibility ≠ permission.)
3. **Per-field canonicalization** both sides before comparison: `acting_as` via
   `resolve_principal` (so a skill gated on `self` matches any install's assistant
   name — `emi`/`aria`/`me` all canonicalize to `self`). Other fields lowercased-exact.
4. **`requires_scope_acting_as: X`** is back-compat sugar for `requires_scope: {acting_as: X}`.
5. **Two injection routes by keyword presence:**
   - **has `task_keywords`** → *keyword-gated*; fires only where the keyword appears
     (`matching_skill_names`). Rides the turn it matched.
   - **no `task_keywords` + a `requires_scope`** → *always-on for that scope*
     (`always_inject_skill_names`); the scope builder stamps it into
     `scope.skills.always_inject`, which **propagates to every downstream agent** for
     the whole task (it doesn't depend on any agent seeing a keyword).
   - **neither** → inert (static-bind only, 5.1).

#### Worked examples

**1. Keyword-gated contextual skill** — generic how-to, fires by topic, any principal:
```yaml
# skills/github/SKILL.md
metadata:
  auto_inject_when:
    task_keywords: ["github", "pull request", "gh issue"]
# No requires_scope → applies to whoever is acting. Fires when the task mentions GitHub.
```

**2. Surface-gated contextual skill** — keyword + a scope condition:
```yaml
# skills/slack-formatting/SKILL.md
metadata:
  auto_inject_when:
    requires_scope: { surface: slack }     # ONLY when the run's surface is slack
    task_keywords: ["slack", "reply", "message"]
# Injects on Slack responses; mechanically NOT on ui/sms/telegram/email
# (replaces the old prose-only "do not apply elsewhere").
```

**3. Always-on persona skill** — keyword-less + a principal gate:
```yaml
# skills/private/emi-acting-as-herself/SKILL.md   (private — owner persona)
metadata:
  auto_inject_when:
    requires_scope: { acting_as: self }    # NO task_keywords
# Always-on whenever acting as the assistant persona; rides every downstream
# agent via scope.skills.always_inject. Discovered from the registry — there is
# no hardcoded principal→skills map. `self` matches regardless of the assistant's
# configured name.
```

**4. INVALID — permission-gated skill (rejected at parse, fail-loud)**:
```yaml
metadata:
  auto_inject_when:
    task_keywords: ["delete"]
    requires_scope: { authority_level: "99" }   # ❌ PARSE ERROR
# Error: "field 'authority_level' is not allowed. Allowed (identity/context only):
# [acting_as, room_context_id, room_id, surface, visibility]. Permission/authority
# fields are forbidden — skills gate relevance, not authorization."
# A skill must NEVER decide access. Gate on identity/context; let the four-layer
# tool gate decide what's allowed.
```

## 6. SkillRegistry — the runtime service

A DI-registered service that owns all skill loading + matching:

```python
class SkillRegistry:
    def get(self, name: str) -> Skill: ...                        # path 5.1
    def discover(self, query: str) -> list[SkillHeader]: ...      # path 5.2
    def headers(self) -> list[SkillHeader]: ...                   # bulk metadata for indexing
    def reload(self) -> None: ...                                 # re-scan after an edit (UI)
    def validate(self, path: Path) -> ValidationResult: ...       # tooling

class SkillInjector:                                              # path 5.3/5.4 matching
    def matching_skill_names(self, *, task, incoming_message,
                             scope=None, scope_acting_as=None) -> list[str]: ...
        # keyword-gated skills whose requires_scope (if any) matches the live scope
    def always_inject_skill_names(self, *, scope=None,
                                  scope_acting_as=None) -> list[str]: ...
        # keyword-LESS skills whose requires_scope matches → scope.skills.always_inject
```

At startup the registry:

1. Walks `skills/` for directories containing `SKILL.md`.
2. Parses YAML frontmatter, validates per the agentskills.io rules (name regex, description length, dir-name match, optional fields well-typed).
3. Populates an in-memory index keyed by `name`.
4. Logs validation errors loudly; a malformed skill is excluded but doesn't crash startup.

`Skill` is a Pydantic model with the parsed frontmatter fields plus a method to read the body. `SkillHeader` is metadata-only (used for discovery without loading the body — the progressive-disclosure tier 1).

## 7. Implementation phases

Building all three injection patterns at once would over-engineer v1. Phasing:

**v1 — foundation + static binding (this work)**

- `skills/` directory layout.
- `SkillRegistry` with `get`, `headers`, `validate`.
- SKILL.md parser + agentskills.io validator.
- Static-binding injection: agent config gets a `skills:` field, prompt context exposes `{{ skills.<name> }}`.
- Migrate `resource_slack_format.md` → `skills/slack-formatting/SKILL.md`. First concrete instance.
- Tests for the registry and validator.

**v2 — tool-discovery**

- `find_skill` and `load_skill` tools wired into the relevant manager planners.
- `SkillRegistry.discover(query)` does description-match (LLM-driven or simple keyword scoring; start with the latter).

**v3 — context auto-injection**

- `SkillInjector` parallel to `PodInjector`. Trigger rules in skill metadata or a separate routing config.
- Move `slack-formatting` from static binding to auto-inject when `room_surface == "slack"`.

**v4+ — frontier**

- **Skill-as-pod**: package a skill directory as a pod, transmit between agents (uses the courier mode already built for `send_email`).
- **Self-authoring**: Sensei writes new skills from successful traces (closes the learning loop — see [TASK_SKILL_DESIGN.md](TASK_SKILL_DESIGN.md) for design context).
- **Inheritance / composition**: child skills extend parents. `doordash-starbucks` inherits `doordash-ordering` inherits `playwright-modals`.

## 8. Design context

The original task/skill design discussion is preserved at [TASK_SKILL_DESIGN.md](TASK_SKILL_DESIGN.md). It pre-dates the agentskills.io standard adoption — useful for the reasoning behind the task-vs-skill split and the learning loop, but the storage format and selection mechanism are now anchored on the open standard documented above.
