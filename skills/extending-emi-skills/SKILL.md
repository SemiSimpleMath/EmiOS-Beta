---
name: extending-emi-skills
description: How to add a new skill to EmiOS. A skill is markdown action-instructions for agents — agentskills.io SKILL.md format. Use when the task involves authoring shareable agent guidance, keyword-triggered helpers, or pod-shareable instruction artifacts.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "new skill"
      - "add skill"
      - "create skill"
      - "skill.md"
      - "agent skill"
      - "extend emi skills"
---

# Adding a new skill

Skills live in `skills/<name>/SKILL.md`. The `SkillRegistry` loads
them at startup. Drop the directory, restart Flask, the skill is
discoverable.

## File shape — two roots

```
skills/<name>/SKILL.md            # public: tracked, shipped with the repo
skills/private/<name>/SKILL.md    # private: GITIGNORED, per-user
```

Both roots are scanned identically and merged into one index keyed by skill name
(the public scan skips the `private` directory, then scans it as its own root).
`skills/private/` is where persona / `acting_as` skills live — the ones that must
never reach the public repo. Because both land in one name-keyed index, a private
skill **shadows** a public one of the same name.

Directories starting with `.` or `_` are skipped by convention. A directory with
no `SKILL.md` is logged and skipped.

Optional supporting files (referenced from the skill body) can sit
in the same directory.

## SKILL.md frontmatter

Follows the agentskills.io spec. Frontmatter is YAML; everything
after the closing `---` is the markdown body that gets injected.

```yaml
---
name: my-skill                       # lowercase a-z 0-9 -, 1-64 chars, MUST match dirname
description: One-line that triggers usage. The HEADER (name + description) is always in agent context for tier-1 discovery; the body loads only on activation.
license: Apache-2.0
compatibility: emi-1                 # optional
allowed_tools: ""                    # accepted for spec portability; NOT enforced — tool permission comes from the scope contract
metadata:
  author: jukka
  version: "1.0"
  auto_inject_when:                  # how the skill enters an agent's prompt
    task_keywords:
      - "keyword1"
      - "keyword2"                   # case-insensitive WHOLE WORD / phrase
    requires_scope:                  # optional gate — identity/context only
      acting_as: self
---
```

### Keyword matching is whole-word, never substring

`task_keywords` match with word boundaries on both sides
(`(?<!\w)keyword(?!\w)`), lowercased on both sides. So `"pr"` matches
`open a pr` but **not** `dicaprio` or `prince`. A keyword that is a fragment of a
longer word will never fire — write the word or phrase you actually mean.

### `requires_scope` — the lock the scope is the key to

`auto_inject_when.requires_scope` gates a skill on the LIVE scope. Every listed
field must match (AND). Allowed fields are identity/context only:

`acting_as` · `surface` · `room_id` · `room_context_id` · `visibility`

Permission fields (tools, pods, approval, writes, resources, entities) are
**rejected at parse time with an error** — skills gate *relevance*, never
authorization. `requires_scope_acting_as: self` is sugar for
`requires_scope: { acting_as: self }`.

Two shapes follow from this:

- **keywords + gate** → auto-injects when the keyword hits *and* the scope matches
- **gate + NO keywords** → **always on** whenever that scope is active
  (`always_inject_skill_names`). This is how persona / `acting_as` skills work;
  see `skills/private/` for the live examples.

A skill with neither keywords nor a gate is static-bound only: it is injected
when an agent names it, and is still discoverable.

## Five ways skills land in an agent's prompt

1. **Per-agent static binding** — agent's `config.yaml` has
   `skills: [my-skill]`. Loaded every run of that agent.
2. **Keyword-triggered** — `metadata.auto_inject_when.task_keywords`
   matches (whole-word) against the agent's task + incoming message.
3. **Scope-gated always-on** — `requires_scope` with no `task_keywords`;
   injected across every downstream agent whenever that scope is active.
4. **Pod-shared** — an agent mints the skill as a pod and another
   agent picks it up by reference. Same markdown shape; pods are
   the transport.
5. **Discoverable** — agents can call `SkillRegistry.discover(query)` and pull
   any skill on demand. No prior declaration needed.

Whichever path a skill arrives by, it must pass `skill_gate_passes` — the one
universal check that its `requires_scope` matches the live scope. A skill with no
`requires_scope` passes trivially.

`discover()` is **keyword scoring, not semantic search**: query tokens of 3+
characters with stopwords removed, scored as `name_hits × 3 + description_hits`,
top 5 by default. Write descriptions containing the literal words someone would
search for — an embedding-free ranker cannot infer synonyms.

## Body shape

Treat the body as direct prompt content. Lead with **when this
skill applies** (so the agent knows whether to act on it), then
**what to do**, then optional examples.

```markdown
# What this skill is

One sentence.

## When this applies
- Bullet conditions.

## What to do
- Step / rule / pattern.

## Examples
- Concrete example with input → expected behavior.
```

Keep it tight. Agents are reading this WHILE deciding what to do
— exposition costs them tokens and attention.

## After dropping the file

1. Restart Flask — there is no automatic re-scan. (The `/skills` editor calls
   `SkillRegistry.reload()` after an edit, so a body change made there takes
   effect without a restart. The new index is built aside and swapped in one
   assignment, so a concurrent prompt never sees a partial registry.)
2. Verify in logs: `[skill_registry] loaded N skill(s) (X public, Y private),
   skipped Z` — and check Z is what you expect.
3. View on `/skills/all` — it appears in the right bucket
   (per-agent, keyword-triggered, or shared) based on usage.

## Validating — what blocks loading vs what only warns

**Errors — the skill is excluded from the registry** (logged, but startup
continues):
- `name` missing, over 64 chars, not `[a-z0-9-]`, edge hyphens, or `--`
- **`name` not equal to the parent directory name** — the registry indexes by
  name, so a mismatch would silently miswire
- `description` missing/blank or over 1024 chars
- malformed YAML frontmatter
- `metadata.auto_inject_when` not a mapping; `task_keywords` not a list of
  non-empty strings; `requires_scope` naming a non-allowlisted (permission) field
- `license` / `compatibility` / `metadata` / `allowed-tools` of the wrong type

**Warnings only:**
- an empty body ("skill will inject nothing useful")
- `allowed-tools` present — accepted for agentskills.io portability but **not
  enforced**; tool permission comes from the scope contract, never from a skill

Duplicate names are **not** a parser check: the parser sees one file at a time.
Since `name` must equal the directory name, duplicates can only arise across the
two roots — where the private copy deliberately shadows the public one.

## Canonical examples

- Pure how-to: `skills/critic-handling/SKILL.md`
- Keyword-triggered (single domain): `skills/bbc-site/SKILL.md`
- Pod-aware skill: `skills/pod-courier/SKILL.md`
- User-editable instruction: `resources/instructions/resource_email_user_prefs.md`
  (surfaced via the dedicated `/skills/email` editor)

## Notes

- "Discoverable" isn't a separate file shape — every loaded skill
  is queryable. Skills opt INTO keyword auto-trigger by setting
  `auto_inject_when.task_keywords`; they're discoverable either way.
- Don't repeat content already in `app/assistant/agents/<agent>/prompts/system.j2`.
  If three agents say the same thing in their prompts, that's a
  shared skill candidate.
- For user-editable instructions (email handling, dayflow overrides)
  add a dedicated editor page in `app/routes/preferences.py` and
  link from `/skills` — see `/skills/email` and `/skills/dayflow`
  for the pattern.
