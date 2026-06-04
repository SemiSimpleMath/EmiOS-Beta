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

## Layer 2 — Access = PRINCIPAL **and** AUTHORITY (policy, not storage)

Enforced at the **surfacing/use** layer (where accounts are shown to / invoked by an
agent), reading the account **entry** (metadata) and the **scope**. Never at the mint.

An agent may see/use an account **iff BOTH gates pass**:

1. **Principal — `accessible_by`.** The account lists which principals may use it
   (`accessible_by`). The surfacing gate matches the scope's current acting-as principal
   (set by `/actas`, canonicalized by `resolve_principal`) against that list.
   (`accounts_for_principal` already does this filter.)
   > `owner` (who the account is *for*) is **separate metadata** — not the same field as
   > `accessible_by`. Whether acting-as the owner *grants* access is an explicit,
   > undecided **policy**; do not weld `owner` into the access check.
2. **Authority — the int dial.** `scope.authority_level ≥ account.authority`. Per-account
   number (anchors `PUBLIC 10 / CHAT 50 / GATED 70 / USER 99 / COURIER 100`; typical
   `master_room` = 99). A free/throwaway account may sit lower (less approval friction);
   an ultra-sensitive one at 100 (no agent ever references it). *(This filter is the part
   still to be added to the surfacing path.)*

Neither gate touches the bytes-band (always 100) or the mint.

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
- **Access path** (Layer 2): `/actas <principal>` sets the scope principal → surfacing
  gate checks `principal ∈ accessible_by` **and** `scope.authority ≥ account.authority`
  → approval gates the action.

The two paths **never cross**. That is the whole design.

---

## Locked invariants (a passing comment does not move these)

1. Bytes never reach an LLM; `full`@100/courier; **the mint is uniform**.
2. Pods are pointers to `.env` names, not bytes.
3. Access = **principal (acting-as ∈ `accessible_by`) AND authority (dial)** — a
   surfacing/use **policy**, never a mint variation. `owner` is metadata, distinct from
   `accessible_by`; an owner→access rule is an explicit open policy, not identity.
4. `.env` = source of truth for values; registry = metadata; pages = projections.
5. Authority is an `int` (ordered line), not an enum; the **99/100 cap** is the only
   categorical line.

## Status (2026-06-04)

- **Built & committed:** env page reads the real `.env` masked (`105095e1`); pod
  lifecycle mint-on-`agent` / hard-delete-on-demote + per-var sensitivity on the env page
  (`91693b2e`).
- **Designed, not built:** account creation (`owner/login/password/authority`) with the
  **uniform** mint + `authority`/`accessible_by` on the entry; the **authority** half of
  the Layer-2 surfacing gate (the principal half exists via `accounts_for_principal`).
- **Queued:** names (`PRIMARY_USER`/`ASSISTANT_NAME`) out of `.env` → resource files,
  fold the two name resolvers (separate change).
