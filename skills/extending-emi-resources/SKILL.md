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

The `resource_manager` walks `resources/` recursively at boot and loads every
`.json`, `.md`, `.txt` or extension-less file it finds. **The resource id is the
filename stem** — the `resource_` prefix is a convention the loader does not
enforce, so `my_thing.md` would register as `my_thing`. Keep the prefix anyway;
every consumer expects it.

Skipped during the scan: hidden files and directories, `README*`, and anything
under `tmp/`, `tests/`, `__pycache__/`, `templates/`, `day_context/` or
`pointers/`. A `*_personal.*` file is never a standalone resource (see overlays).

> **Resources must be CONCRETE.** A loaded value containing `{{` or `{%` is
> rejected with an error — `Resource 'X' is not concrete`. Jinja belongs in
> `resources/templates/**`, which the loader compiles into real resources in a
> separate phase (same relative path, minus `templates/`). Putting a Jinja token
> in an instruction file is a hard failure, not a passthrough.

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

The validator checks that loop at boot, but it only **warns** — it does not fail.
A declared-but-unreferenced context item, or a `{{ resource_X }}` in a prompt that
nothing declares, logs a warning and the process starts normally. So the loop is
*reported* end-to-end at boot; reading the warnings is on you.

## Personal overlays (gitignored, appended at runtime)

For the user's private edits on top of a public template:

```
resources/instructions/resource_<name>.md           # public, tracked
resources/instructions/resource_<name>_personal.md  # private, gitignored
```

The resource_manager appends the `_personal` content **after** the public base
(`base + "\n\n" + personal`) every time the file is read from disk — initial load
and every reload alike, via `_read_with_overlay`. Later instructions win at LLM
read time, so personal directives override the template's defaults without
editing the public file. `git pull` doesn't clobber the personal overlay; the
personal overlay doesn't push to the public repo.

Two details that matter:

- **Text only.** The append happens only when base and overlay are both strings,
  so a `.json` resource (which loads as a dict) silently ignores its
  `_personal.json`. Overlays are for markdown/text resources.
- **Editing only the overlay still takes effect.** Staleness is keyed on the
  newest mtime across base *and* overlay, so touching just the personal file
  triggers the reload.

Pattern in use:
- `resource_orchestrator_user_prefs.md` (public template)
- `resource_orchestrator_user_prefs_personal.md` (Jukka's private overrides)

## What's NOT drop-in (deeper architecture)

These resource kinds exist but are not added by dropping a file:

- **Pipeline outputs** — `resources/dayflow_pipeline_outputs/*.json`,
  `resources/daily_insights_pipeline_outputs/*.json`,
  `resources/kg_derived/*.json`. Written by background pipelines.
  Adding a new one means writing a pipeline step that populates it.
- **Computed-on-demand** — two flavours. Either a routine/pipeline writes the
  file (e.g. `resource_weather`), or the resource has **no file at all** and is
  computed per read by a registered provider:
  `resource_manager.register_provider("resource_x", lambda scope: …)`. The
  provider receives the live scope, so the value can differ per caller —
  `resource_accounts` and `resource_email_accounts` work this way. Providers are
  registered in `bootstrap`, not by dropping a file.
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
├── dayflow_pipeline_outputs/    <- pipeline output (DON'T HAND-EDIT)
├── daily_insights_pipeline_outputs/  <- pipeline output
├── kg_derived/        <- KG-projected resources (DON'T HAND-EDIT)
├── identity/          <- user/assistant identity files
├── templates/         <- Jinja SOURCES, compiled into resources (not loaded directly)
├── day_context/       <- NOT LOADED as resources (skipped by the scan)
└── pointers/          <- NOT LOADED as resources (skipped by the scan)
```

For a new user-editable resource: `resources/instructions/`. For
anything else, you're in deeper-architecture territory.

## After dropping the file

1. Declare it in any agent's `*_context_items` that should see it.
2. Reference it in that agent's prompt template.
3. Restart Flask — needed to **discover a new file**. Editing an existing
   resource does **not** need a restart: reads compare the file's mtime against
   the cached one and auto-reload when it advances (`Resource 'X' auto-reloaded
   after file mtime advanced`).
4. Check startup logs for `✅ Loaded text resource 'X'` and for validator
   warnings about a declared item missing from the prompt (a warning, not a
   failure — see above).

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

- Resource names must be globally unique — the id is the filename stem and the
  namespace is flat. On a collision the loader prefers a canonical subfolder file
  over a root-level one (with a warning) and otherwise skips the duplicate (also
  with a warning). Two `resource_weather.json` files are a hard error.
- Reads are scope-gated: `get_resource` raises `PermissionError` unless the
  resource is in the scope's `allowed_global_resources` (or it lists `all`), and
  an explicit `denied_resources` entry always wins. A resource can additionally
  carry a `requires_scope` lock (`register_lock`) — `resource_user_email` is
  locked to `acting_as: user` so it cannot leak into self/assistant mode.
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
