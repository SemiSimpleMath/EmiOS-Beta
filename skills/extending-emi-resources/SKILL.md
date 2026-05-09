---
name: extending-emi-resources
description: How to add a new resource to EmiOS — data that gets injected into agent prompts as {{ resource_<name> }}. Static markdown is fully drop-in; computed/pipeline-output resources are deeper architecture. Use when the task involves authoring agent context, instructions, or persona snippets.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "new resource"
      - "add resource"
      - "agent instructions"
      - "agent context"
      - "persona snippet"
      - "extend emi resources"
---

# Adding a new resource

A **resource** is data injected into an agent's prompt as a Jinja
variable like `{{ resource_email_user_prefs }}` or
`{{ resource_assistant_data.name }}`. The cleanly drop-in case is
**static markdown** — drop a file, declare it in agent config,
reference it in the prompt. Three steps, no code.

> **Naming convention**: file/key prefix is always `resource_`. The
> `{{ resource_X }}` token in templates and the `resource_X` entry
> in `*_context_items` lists must match the filename stem.

## Drop-in: static markdown resource

```
resources/instructions/resource_<name>.md
```

The `resource_manager` auto-discovers any `resource_*.md` (or `.json`)
file under `resources/`. After a restart the value is in the global
blackboard under that exact key.

### Three places that wire it together

1. **The file** — the source of truth.
   ```
   # resources/instructions/resource_my_thing.md
   # My thing
   - Bullet 1
   - Bullet 2
   ```

2. **The agent's `config.yaml`** — declares the agent wants this
   resource in its prompt context.
   ```yaml
   system_context_items:        # or user_context_items
     - resource_my_thing
   ```

3. **The prompt template** — actually renders it.
   ```jinja2
   ## Things you should know
   {{ resource_my_thing }}
   ```

The validator at startup will fail loud if a context item is declared
but not referenced in the corresponding prompt. So the loop is closed
end-to-end at boot.

## Personal overlays (gitignored, appended at runtime)

For the user's private edits on top of a public template:

```
resources/instructions/resource_<name>.md           # public, tracked
resources/instructions/resource_<name>_personal.md  # private, gitignored
```

The resource_manager appends the `_personal.md` content to the public
version at LLM read time. `git pull` doesn't clobber the personal
overlay; the personal overlay doesn't push to the public repo.

Pattern in use:
- `resource_orchestrator_user_prefs.md` (public template)
- `resource_orchestrator_user_prefs_personal.md` (Jukka's private overrides)

## What's NOT drop-in (deeper architecture)

These resource kinds exist but are not added by dropping a file:

- **Pipeline outputs** — `resources/dayflow_pipeline_outputs/*.json`,
  `resources/daily_insights_pipeline_outputs/*.json`,
  `resources/kg_derived/*.json`. Written by background pipelines.
  Adding a new one means writing a pipeline step that populates it.
- **Computed-on-demand** — e.g. `resource_weather`, populated by
  the weather routine. Adding a new computed resource = writing the
  routine that updates it.
- **Live state** — e.g. `resource_dayflow_status`, written
  continuously by the orchestrator.

If you want a resource the user can EDIT directly (instructions,
preferences, persona), use the static-markdown drop-in path. If you
want a resource that the system computes (weather, schedule, beliefs),
that's a routine handler or pipeline step — see
`extending-emi-routines`.

## Featured editor pages

Some user-editable resources have dedicated editor pages already:

- `resource_email_user_prefs.md` → editor at `/skills/email`
- `resource_orchestrator_user_prefs_personal.md` → editor at `/skills/dayflow`

Add similar editor pages in `app/routes/preferences.py` when a
resource is important enough to warrant a first-class UI surface
(see how the Skills routes expose those two for the pattern).

## Subdir conventions

```
resources/
├── instructions/      <- static markdown (DROP-IN ZONE)
├── assistant/         <- assistant identity / persona JSON
├── context/           <- shared context blobs
├── day_context/       <- daily-context resource (written by pipelines)
├── dayflow_pipeline_outputs/    <- pipeline output (DON'T HAND-EDIT)
├── daily_insights_pipeline_outputs/  <- pipeline output
├── kg_derived/        <- KG-projected resources (DON'T HAND-EDIT)
├── identity/          <- user/assistant identity files
└── pointers/          <- pointer/index files
```

For a new user-editable resource: `resources/instructions/`. For
anything else, you're in deeper-architecture territory.

## After dropping the file

1. Declare it in any agent's `*_context_items` that should see it.
2. Reference it in that agent's prompt template.
3. Restart Flask.
4. Check startup logs — the agent_validator will complain loud if
   a declared resource isn't actually referenced in the prompt
   (the typo guard).

## Canonical examples

- **Pure static markdown**: `resources/instructions/resource_kg_principles.md`
  — agent guidance, no overlay, no computation.
- **Public + personal overlay**:
  `resources/instructions/resource_orchestrator_user_prefs.md`
  + `resources/instructions/resource_orchestrator_user_prefs_personal.md`
- **User-editable with dedicated editor**:
  `resources/instructions/resource_email_user_prefs.md`
  → exposed at `/skills/email`.

## Notes

- Resource names must be globally unique. The blackboard is one
  flat namespace.
- `{{ resource_X }}` and `{{ resource_X.field }}` both work — JSON
  resources expose nested fields; markdown resources are strings.
- Prompts that reference a resource the agent didn't declare in
  context_items will render as empty string (the value isn't in the
  agent's local context). Always declare in `*_context_items`.
- See also `extending-emi-skills` for the difference between a
  RESOURCE (data) and a SKILL (action instructions). Some content
  could fit either; the rule of thumb: if it tells an agent HOW to
  act, it's a skill; if it tells an agent WHAT to know about, it's
  a resource.
