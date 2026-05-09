---
name: emi-ui-templating
description: UI templating conventions for EmiOS Flask templates. The biggest rule is "never hardcode the assistant name" — every user picks their own (Emi is just the default). The Jinja variable {{ assistant_name }} is injected globally; use it everywhere a user-visible string would otherwise read 'Emi'. Use when adding or editing any HTML template under app/templates/.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "new template"
      - "edit template"
      - "html template"
      - "jinja"
      - "ui template"
      - "page title"
      - "assistant name"
---

# UI templating in EmiOS

## Rule 1 — Never hardcode "Emi" in any user-visible string

The assistant's name is **per-user**. Every install picks one through the
setup wizard and the user can rename later. "Emi" is just the seed default.

The canonical Jinja variable is `{{ assistant_name }}` and it's injected
globally via a Flask `context_processor` in `app/create_app.py`. Every
template — extending `base.html` or standalone — has it in scope; you do
**not** need each route to pass it explicitly.

### Where to apply

Anywhere a string would name the assistant, replace it with the variable:

```jinja2
<!-- WRONG -->
<title>Emi</title>
<title>Emi - Document Editor</title>
<h1>Personalize Emi</h1>
<p>How Emi handles your inbox.</p>

<!-- RIGHT -->
<title>{{ assistant_name }}</title>
<title>{{ assistant_name }} - Document Editor</title>
<h1>Personalize {{ assistant_name }}</h1>
<p>How {{ assistant_name }} handles your inbox.</p>
```

### Where it's OK to keep "Emi"

- **CSS class names**: `.emi-card`, `emi.css`, `emi-components.css`. These
  are internal identifiers, not displayed text. Don't rename.
- **Brand strings tied to the project itself**: `EmiOS`, `EmiAi`,
  `EmiCode`. The product name doesn't rename when the assistant does.
- **Code module names**: `Emi.js`, `app/static/js/emi/`. Same as CSS.
- **Setup wizard placeholder**: `value="Emi"` as the *default* in the
  name input is correct — it's what the user sees on first run before
  they pick a name.
- **Comments**: Jinja comments `{# ... #}` and HTML `<!-- ... -->`. The
  file-level comments in `_top_bar_user_menu.html` etc. are documentation
  for future readers; they can name "Emi" since they're describing the
  product, not displaying it. Use judgment — if a comment becomes a
  user-facing tooltip later, switch to the variable.

## Rule 2 — Where the variable comes from

```python
# app/create_app.py
@app.context_processor
def _inject_assistant_name():
    from app.assistant.utils.assistant_name import get_assistant_name
    return {"assistant_name": get_assistant_name()}
```

`get_assistant_name()` resolves in this order:

1. `ASSISTANT_NAME` env var (set by the UI settings page or `.env`)
2. `name` field in `resources/assistant/resource_assistant_data.json`
3. Default literal `"Emi"`

The fallback exists so a fresh checkout still renders something readable
before the user has run setup.

## Rule 3 — Block titles in `base.html` extenders

`base.html` defines:
```jinja2
<title>{% block title %}{{ assistant_name }}{% endblock %}</title>
```

When a child template overrides the block, propagate the variable:

```jinja2
{% extends 'base.html' %}
{% block title %}Music - {{ assistant_name }}{% endblock %}
```

Don't write `{% block title %}Music - Emi{% endblock %}`.

## Rule 4 — Other globals to assume in scope

Same context processor mechanism gives you these without each route
passing them:

| Variable        | Source                                      |
|-----------------|---------------------------------------------|
| `assistant_name`| `get_assistant_name()` (this skill)         |
| `static_v`      | per-process startup timestamp (cache-bust)  |

Routes that already pass `assistant_name=...` explicitly (chat_bot.py,
emi_code.py, kg_dev.py) keep working — explicit kwargs win, and the
context processor is just the fallback. New routes don't need to pass it.

## After editing templates

1. Restart Flask (or rely on Jinja autoreload if dev mode).
2. Set `ASSISTANT_NAME` to something other than "Emi" and reload — every
   page should render with the new name. Anything still saying "Emi"
   missed the rule.

## Related

- `app/assistant/utils/assistant_name.py` — the resolver
- `app/create_app.py` — the global context processor
- `extending-emi-resources` skill — for prompt context (different layer
  from UI templating but follows the same "never hardcode" principle)
