# Secrets, Accounts & Authority

Canonical reference for how secrets, accounts, pods, authority, and `/actas` fit
together. This is one system — the **env registry** — with three layers that must
never blur. If a change seems to move one layer's job into another, it's wrong.

> The one idea: a secret is a `.env` variable. An account is a secret that belongs
> to a **person**. Confinement is uniform; access is a policy; the pages are views.

---

## Layer 1 — Confinement (UNIFORM; never varies by sensitivity)

- **Values live in `.env`** (on disk, gitignored). The registry holds **metadata only**
  (names + display) — never values.
- **A pod is a POINTER to a `.env` var name (`env_ref`), never the bytes.** The
  `auth.bearer` materializer records the `full` projection as
  `storage_kind="env", env_ref=<name>`.
- **`full` projection = authority 100 (`AUTH_COURIER`).** No LLM ever reads it. The
  **courier** — deterministic substitution code (e.g. `http_request._resolve_pod_ref`
  → `ScopeAdapter.for_courier_call`, authority 100) — dereferences `pod_id/full →
  os.getenv(env_ref)` at execution, into the tool's Python scope, **never the prompt**.
- **Every secret/account is minted IDENTICALLY.** The mint does **not** depend on the
  account's sensitivity. (Sensitivity is enforced in Layer 2, not here.) Changing the
  mint per-account is the one thing this design forbids.

## Layer 2 — Access via a scope-gated RESOURCE (policy, not storage)

Accounts reach an agent ONLY as a **dynamic resource** (e.g. `resource_accounts`),
resolved through the same scope-gated path as every other resource — `resolve_resource`
→ `ResourceResolver.get_global_resource(..., scope_context)`. They are **not**
auto-injected. Two conditions, both required (the existing `resource_*` contract):

1. **Opt-in** — the agent lists the resource in its `config.yaml` context items and
   references it in its `.j2` template. No request → no accounts.
2. **Scope grant** — the scope's resource policy must grant it (the **lock** in the
   scope=key / resource=lock capability model). A scope that doesn't grant it gets
   nothing (degrades to empty).

That outer gate **is** the access control. *Inside* the computed resource value, two
filters shape WHICH accounts appear:
- **Principal** — only accounts whose `accessible_by` includes the current acting-as
  principal (`/actas`, via `resolve_principal`).
  > `owner` (who the account is *for*) is **separate metadata** — not the same field as
  > `accessible_by`. Whether acting-as the owner *grants* access is an explicit,
  > undecided **policy**; do not weld `owner` into the access check.
- **Per-account authority** — the account's `authority` int (anchors `PUBLIC 10 / CHAT 50
  / GATED 70 / USER 99 / COURIER 100`; typical `master_room` 99). A free account may sit
  lower; an ultra-sensitive one at 100.

None of this touches the bytes-band (always 100) or the mint. Accounts are a **dynamic**
resource — same scope-gated path as a file resource, but the value is COMPUTED from the
registry (`render_accounts_for_scope`), not read from disk.

**Dynamic-resource registration (SHIPPED).** Resources are **lock-optional**, mirroring
skills: a resource may declare a `requires_scope` lock evaluated by the SAME shared
scope-gate primitive skills use (`utils/scope_gate.scope_gate_passes`) — **no lock = free**
(backward-compatible; every existing file resource stays unlocked). `ResourceManager` carries
a **provider registry** (`register_provider`, computed value) and a **lock registry**
(`register_lock`), both optional. `get_resource(*, scope_context, resource_id)` runs the
order: `denied_resources` → the `allowed_global_resources` allowlist (`"all"` or explicit) →
the lock (if any) → then resolves the value (provider if registered, **else** file/cache).
Account resources register a provider (some also a lock); the `available_accounts`
special-case auto-inject in `context_injector` is **deleted** (the only remaining mention is
the bootstrap comment recording its removal). Agents reach accounts only through the normal
`resolve_resource` → `ResourceResolver.get_global_resource(..., scope_context=...)` path.

**The domain-relevance (`categories`) filter.** `render_accounts_for_scope(scope, *, categories=None)`
takes an optional set of platform categories (`gmail`→`email`, `bluesky`→`social`, … per the
`_FEATURE_BY_PLATFORM` map, unknowns → `other`). When set, only accounts whose platform
category is in the set are rendered. This is **RELEVANCE, not permission** — principal +
authority still gate *access*; `categories` only trims to what is pertinent to the agent's
job. `personal_admin::planner` requests `{"email"}` (via the `resource_email_accounts`
provider) so social accounts never enter its prompt; `categories=None` (the `resource_accounts`
provider) shows every category the scope already permits.

**Why authority is an `int`, not an `enum`:** it's an ordered line and the gate is a
single `caller ≥ required` comparison. The named bands are anchors; a pod/account can
sit anywhere on the line, and a materializer can place a projection anywhere. The only
**categorical** point is the **99/100 cap** — the line no LLM crosses. Everything below
99 is a soft gradient.

## Layer 3 — Surfaces (two zoom levels on the one registry)

- **`/settings/accounts`** — people's accounts: `owner · login · password · authority`.
  Friendly, guided. Create → mint the (standard) pod + write the entry.
- **`/settings/env`** — every `.env` var, masked, dev-level, with each var's sensitivity
  + pod correspondence. Superset. Account-backing vars are **read-only** here
  ("managed on Accounts").
- Same `EnvRegistryService`: `builtins.json` (shipped) + `env_registry_user.json`
  (gitignored, UI-written), merged per-field. Account = `kind=account` entry; plain
  secret = `value`/`secret` entry. The env page's `config / courier / agent` **is**
  Layer-2 authority applied to a plain var (`agent` = surfaced/minted, `courier` =
  code-only, `config` = not a secret).

---

## Storage (LOCKED)

- **`.env`** — flat `key=value`, **values only**: `ACCT_<owner>_<platform>_HANDLE=…`,
  `ACCT_<owner>_<platform>_SECRET=…`. No structure ever lives here.
- **`env_registry_user.json`** (gitignored, UI-written) — the **unified** registry:
  structured records for **both** plain env-var metadata and **accounts**
  (`kind=account`). It points at `.env` keys *by name*; it never stores a value. Shipped
  defaults live in `builtins.json`; the two merge per-field.
- **Accounts do NOT get a separate file** — they are registry entries. (The old dedicated
  `resource_emi_accounts.json` was retired when the registry was unified; it is not
  being reintroduced.)

---

## The account entry (`kind=account`)

| field | meaning |
|---|---|
| `owner` | recorded metadata — who the account is *for* (user / family member / `self`). Pure data, distinct from `accessible_by`. |
| `accessible_by` | principals that may use it (matched against the acting-as principal). Set explicitly; its relationship to `owner` is an open policy. |
| `platform`, `label` | display + routing |
| `handle_env` | `.env` var holding the login / handle |
| `auth.kind` | `pod_ref` (paste secret → pod) \| `google_oauth` (redirect, no pod) |
| `auth.env_ref` | `.env` var holding the password / secret |
| `auth.pod_id` | the minted pod (a pointer) |
| `authority` | the Layer-2 dial (int) |

## One account, end to end

Create: `owner + login → *_HANDLE`; `password → *_SECRET → standard pod (full@100)`;
`authority → on the entry`.

- **Bytes path** (Layer 1): `.env` → pod pointer → courier@100 → tool. Uniform.
- **Access path** (Layer 2): the agent opts into the `resource_accounts` resource
  (`config.yaml` + `.j2`); the scope's resource policy gates it; the computed value lists
  only accounts where `principal ∈ accessible_by` **and** `scope.authority ≥ account.authority`;
  the agent pastes a pod-ref into a tool call; the courier substitutes; approval gates the action.

The two paths **never cross**. That is the whole design.

---

## Locked invariants (a passing comment does not move these)

1. Bytes never reach an LLM; `full`@100/courier; **the mint is uniform**.
2. Pods are pointers to `.env` names, not bytes.
3. Accounts surface ONLY as a **scope-gated resource** (opt-in via config.yaml/.j2 + the
   scope's resource policy) — never auto-injected. Principal (`accessible_by`) and
   per-account authority filter WHICH accounts appear inside it. A surfacing **policy**,
   never a mint variation. `owner` is metadata, distinct from `accessible_by`; an
   owner→access rule is an open policy, not identity.
4. `.env` = source of truth for values; registry = metadata; pages = projections.
5. Authority is an `int` (ordered line), not an enum; the **99/100 cap** is the only
   categorical line.

## Status (2026-06-16)

The whole three-layer design is **shipped and wired**. The dynamic-resource registration
that was "the open plumbing question" is resolved.

- **Accounts-as-a-dynamic-scope-gated-resource — SHIPPED.** `app/bootstrap.py` registers, at
  init, two providers backed by `EnvRegistryService.render_accounts_for_scope`:
  - `resource_accounts` → `render_accounts_for_scope(scope)` (every permitted category).
  - `resource_email_accounts` → `render_accounts_for_scope(scope, categories={"email"})`
    (the domain-relevance filter; `personal_admin` consumes this).

  Both apply the **principal** filter (`accessible_by` ∋ `acting_as`, via `resolve_principal`)
  **and** the **per-account authority** filter (account `authority` int ≤ caller's
  `approval.authority_level`) *inside* the computed value. Access itself is the outer
  scope-gate (`get_resource`'s allow/deny + lock) — accounts surface only when the agent
  opts in (`config.yaml` context item + `.j2`) **and** the scope grants the resource.
  `bootstrap.py` also `register_lock("resource_user_email", {"acting_as": "user"})` — locking
  the user's email-identity (file) resource to user-mode so it does not leak into `/actas self`
  (where email-as-self governs instead).
- **`create_account()` — SHIPPED.** `EnvRegistryService.create_account(owner, platform, login,
  secret, authority, …)` writes name-neutral `ACCT_…_HANDLE`/`ACCT_…_SECRET` `.env` keys
  (values only), mints the **standard** `auth.bearer` pod (uniform mint — `authority` is *not*
  applied at mint; it lands on the entry as surfacing policy), and persists the `kind=account`
  record to `env_registry_user.json`.
- **`resolve_gmail_account_id(alias, *, scope_acting_as)` — SHIPPED.** Resolves a planner
  alias / scope principal to a Google OAuth `account_id`: `"user"` → the owner's primary
  Google account, `"self"` → the assistant's gmail `account_id` (raises if her gmail isn't
  configured), anything else → passed through as a literal id. Companion:
  `expected_email_for_account_id` validates a consented identity against the registry's
  expected handle.
- **`available_accounts` special-case — REMOVED.** The auto-inject in `context_injector` is
  gone; the sole remaining occurrence in `app/` is the `bootstrap.py` comment recording the
  switch to the provider. (Verified by repo-wide search.)
- **Env/Accounts pages — SHIPPED.** Env page reads the real `.env` masked; pod lifecycle
  mint-on-`agent` / hard-delete-on-demote + per-var sensitivity on the env page.
- **Queued (unchanged):** names (`PRIMARY_USER`/`ASSISTANT_NAME`) out of `.env` → resource
  files, fold the two name resolvers (separate change).
