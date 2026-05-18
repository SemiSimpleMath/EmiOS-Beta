---
name: extending-emi-materializers
description: How to add a new pod materializer to EmiOS. Materializers turn a raw secret value (SSN, API token, OAuth key) into authority-banded projections at pod creation time. Use when the task involves adding a new SECRET-pod type — anything that needs courier-only access to its full value with derived display-tier projections.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "new pod materializer"
      - "add materializer"
      - "secret pod"
      - "identity pod"
      - "auth pod"
      - "api key pod"
      - "bearer token pod"
      - "extend emi materializers"
---

# Adding a new pod materializer

A materializer is the function that runs once when a secret pod is created. It takes the raw value (read from an env var) and computes a set of authority-banded **projections** — derived views of the secret that can be revealed to lower-authority callers without exposing the full value.

This is distinct from a **pod kind** (see `extending-emi-pod-kinds`). Pod kinds classify pods for KG admission and body extraction. Materializers run for SECRET pods specifically — the ones whose full value must never enter an LLM transcript.

## When to add a new materializer

- You need a new identity / credential pod type: `identity.passport`, `auth.oauth_refresh`, `health.insurance_card`, `financial.routing_number`, etc.
- The new type has shape-specific derived projections (e.g., SSN's `last4`, phone's `area_code`, OAuth's `expiry_date`).
- The full value should be courier-only; a partial view should be safe for chat-tier rendering.

**You do NOT need a materializer if** the pod just stores opaque content (an email body, an image) — those go through the regular pod-mint paths (`PodStore.put`), not the secret-pod path.

## The pattern

Drop a file in `app/assistant/pod_store/materializers/`. The package auto-discovers it at import time. **No central-file edit required.**

```python
# app/assistant/pod_store/materializers/my_kind.py
from typing import List
from app.assistant.pod_store.authority import (
    AUTH_PUBLIC, AUTH_CHAT, AUTH_GATED, AUTH_USER, AUTH_COURIER,
)
from app.assistant.pod_store.materializers import ProjectionSpec, register


def materialize(*, raw_value: str, env_ref: str, **_kwargs) -> List[ProjectionSpec]:
    """Compute projections from a raw value.

    Args:
        raw_value: the secret as read from the env var. Used to compute
            derived projections, then released — not persisted.
        env_ref: the env var name. Recorded on the full projection so
            courier reads can re-resolve.

    Raise ValueError on malformed input — the user gets a clear error
    at pod-create time rather than a confused pod with partial projections.
    """
    if not raw_value:
        raise ValueError("my_kind materialize: raw_value is empty")
    # ... your validation here ...

    return [
        ProjectionSpec(
            projection_name="full",
            min_authority=AUTH_COURIER,
            storage_kind="env",
            env_ref=env_ref,
        ),
        ProjectionSpec(
            projection_name="display",
            min_authority=AUTH_CHAT,
            storage_kind="plain",
            plain_value="some derived non-sensitive view",
        ),
        ProjectionSpec(
            projection_name="redacted",
            min_authority=AUTH_PUBLIC,
            storage_kind="plain",
            plain_value="***",
        ),
    ]


register("my.kind", materialize)
```

That's it. Restart Emi; the registry auto-discovers the new file.

## ProjectionSpec fields

- **`projection_name`** — the name agents use to fetch (`"full"`, `"last4"`, `"prefix"`, …).
- **`min_authority`** — band required to fetch this projection. Use named constants:
  - `AUTH_PUBLIC` (10) — anywhere, including display in tickets
  - `AUTH_CHAT` (50) — chat-tier agents, the default
  - `AUTH_GATED` (70) — sensitive-but-shareable; some user surfaces
  - `AUTH_USER` (99) — operations the user verifies in-the-loop
  - `AUTH_COURIER` (100) — non-LLM deterministic code only. The full secret lives here.
- **`storage_kind`** — one of:
  - `"plain"` — value is `plain_value` (low-authority derived projections)
  - `"env"` — value comes from `os.environ[env_ref]` at fetch time (courier-only)
  - `"file"` — value comes from `data/pod_secrets/<file_ref>` at fetch time (courier-only, for large binary secrets)
- **`plain_value`** / **`env_ref`** / **`file_ref`** — populate the one that matches `storage_kind`.

## The "raw_value read once" invariant

At materialize time the code HAS to see the raw value once (to compute derived projections). The contract:

1. `materialize()` receives `raw_value` as a parameter.
2. It computes all derived projections.
3. The full-value projection is recorded as `storage_kind="env"` with `env_ref` pointing back at the env var. NOT as `storage_kind="plain", plain_value=<the raw value>`.
4. After `materialize()` returns, the caller releases its local reference to `raw_value`. No persistence beyond the env var.

So the only enduring storage of the raw value is the env var the user set. The Python process holds it transiently during materialization and forgets.

## Worked examples in the repo

- **`identity_ssn.py`** — 5 projections, regex validation, `area_code` / `last4` / `redacted` / `format` derived from the SSN structure
- **`auth_bearer.py`** — 4 projections for opaque tokens (Bearer/API keys/PATs), with length-only validation and `prefix` / `redacted` / `format` projections

Both files are short (~100 lines each). Copy whichever shape is closer to your new kind.

## Auto-discovery

`app/assistant/pod_store/materializers/__init__.py` runs `_auto_discover()` at import time, walking the package and importing every non-`_` `.py` module. Each materializer file calls `register("kind.name", materialize)` at module top-level; importing the module side-effects the registration.

If your file fails to import (syntax error, missing dep), the loader logs a warning and continues. The rest of the registry still works.

To verify your kind is registered:

```python
from app.assistant.pod_store.materializers import known_types
print(known_types())  # should include "my.kind"
```

## Creating a pod of the new kind

Once your materializer is registered, mint a pod through `PodStore.put_secret_pod`:

```python
import os
from app.assistant.pod_store.pod_store import PodStore

os.environ["EMI_POD_MY_VALUE"] = "the actual secret"
pod_id = PodStore().put_secret_pod(
    pod_type="my.kind",
    owner_subject_id="jukka",
    name="Human-readable label",
    env_ref="EMI_POD_MY_VALUE",
    scope=scope,  # caller's scope; must clear AUTH_USER to mint
)
```

The returned `pod_id` can then be referenced by tools like `web_type_secret` and `http_request` via the `datapod:my.kind:<id>` URI format.

## Testing

Put tests at `app/assistant/tests/tool_tests/test_my_kind_materializer.py`. The pattern (see `test_auth_bearer_materializer.py`) is:

1. Direct `materialize()` calls — assert the projection list shape, authority bands, derived values
2. Round-trip through `PodStore.put_secret_pod` + `fetch_projection` at various scope authorities — assert the courier-only projection refuses chat-tier callers

## When NOT to use a materializer

- Storing a raw email body / chat transcript / image → use `PodStore.put` with the regular `Pod` shape (not the secret-pod path).
- Storing a value that has no derived projections worth banding (just one full value) → still works, just declare only the `full` projection. But consider whether a secret pod is the right pattern — maybe it's just a config entry.
- Adding a new pod **classification** (audio vs video vs document) → that's a pod KIND, see `extending-emi-pod-kinds`.

## Related skills

- `extending-emi-pod-kinds` — adding a pod CLASSIFICATION for KG admission and body extraction
- `extending-emi-tools` — building a tool that USES a pod (resolves it at courier scope, like `web_type_secret` or `http_request`)
