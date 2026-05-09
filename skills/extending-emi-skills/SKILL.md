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

## File shape

```
skills/<name>/
└── SKILL.md
```

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
allowed_tools: ""                    # experimental — space-separated
metadata:
  author: jukka
  version: "1.0"
  auto_inject_when:                  # how the skill enters an agent's prompt
    task_keywords:
      - "keyword1"
      - "keyword2"                   # case-insensitive substring match
---
```

## Four ways skills land in an agent's prompt

1. **Per-agent always-injected** — agent's `config.yaml` has
   `skills: [my-skill]`. Loaded every run of that agent.
2. **Keyword-triggered** — `metadata.auto_inject_when.task_keywords`
   matches against the agent's task or incoming message. Auto-loads.
3. **Pod-shared** — an agent mints the skill as a pod and another
   agent picks it up by reference. Same markdown shape; pods are
   the transport.
4. **Discoverable** — agents can call
   `SkillRegistry.discover(query)` to semantically search and pull
   any skill on demand. No prior declaration needed.

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

1. Restart Flask. SkillRegistry doesn't auto-reload.
2. Verify in logs: `[skill_registry] loaded N skill(s)` includes yours.
3. View on `/skills/all` — it appears in the right bucket
   (per-agent, keyword-triggered, or shared) based on usage.

## Validating

The parser warns on:
- name format violation (caps, spaces, length)
- description >1024 chars
- malformed YAML
- duplicate name across the registry

A malformed skill is logged and excluded — it doesn't block startup.

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
