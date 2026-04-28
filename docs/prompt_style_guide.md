# Prompt Style Guide

Rules for writing and maintaining Jinja2 prompt templates (system.j2, user.j2) across all agents.

## Section headers

- Use `########## SECTION NAME ##########` for major sections
- **One blank line before every section header** (visual separation)
- No blank line after the header — content starts on the next line
- Use `{%-` Jinja whitespace control to prevent blank line accumulation from conditionals

```jinja2
{%- if some_data %}

########## SECTION NAME ##########
- item 1
- item 2
{%- endif %}
```

## Within sections

- No extra blank lines between items
- Use `{%-` on `for`, `if`, `endif`, `endfor` tags to eat trailing newlines
- Bold keys for key-value pairs: `**Key:** value`
- Subsection headers use `**Bold:**` or `##### Heading`

## Date and time

- **All datetime in prompts is LOCAL TIME.** Never show UTC to the LLM.
- Always use `time_utils.py` helpers to convert:
  - `utc_to_local()` for display
  - `get_local_time_str()` for current time
  - `get_local_timezone()` for timezone-aware operations
- The `enrich_item_for_prompt()` utility in `item_display.py` handles timestamp conversion for dayflow items
- LLMs output ISO 8601 with timezone offset (e.g. `2026-04-10T16:55:00-07:00`)
- Never use relative dates in task summaries — write "April 10" not "tomorrow"

## Context item ordering (user.j2)

Standard order for user prompts:
1. **Current state** — time, day, weather, computer presence, location, health
2. **Schedule** — today's calendar events, day theme, milestones, routine
3. **New data** — artifacts, new signals, intake items
4. **Active work** — plans, tasks, tickets
5. **Changes** — recent changes, ticket responses
6. **Completed work** — closed plans, closed tasks
7. **Memory** — chat history, past conversations (RAG), entity context

## Section labels

- RAG/retrieved content: `(RAG — may or may not be relevant)`
- Read-only context: `(read-only, do not act on)` or `(read-only context)`
- User directives: `(user directives — obey these)`
- Completed items: `(do not recreate)`

## System prompt structure

1. Identity/role (who the agent is)
2. Primary responsibilities (what it does)
3. Rules and constraints (what it must/must not do)
4. Output contract (what fields to produce)

Keep system prompts as short as possible. Mini-tier models struggle with long, multi-clause instructions.

## Anti-patterns

- **No content truncation.** Never use `[:100]` or `[:80]` on summaries, synopses, or any content the LLM needs to reason about. List-length limits (`[:40]` items) are fine.
- **No inline LLM calls.** All LLM interactions go through `agent_factory.create_agent()` with config.yaml, agent_form.py, and Jinja2 prompts.
- **No system narration in user-facing text.** "The user is currently at the keyboard and this task is listed as high-priority" is system narration. "Drew needs to bring his Chromebook tomorrow" is user-facing.
- **No multiple-choice questions.** Inform the user of the situation and let them decide.
- **No hardcoded names/times in templates.** Use `{{ resource_user_data.first_name }}`, `{{ resource_assistant_data.name }}`, `{{ date_time }}`.

## Template whitespace recipe

```jinja2
{# Conditional section — no blank lines from the if/endif #}
{%- if data %}

########## SECTION ##########
{%- for item in data %}
- {{ item.summary }}
{%- endfor %}
{%- endif %}
```

The `{%-` eats the newline before the tag. The blank line before `##########` is the only intentional whitespace.
