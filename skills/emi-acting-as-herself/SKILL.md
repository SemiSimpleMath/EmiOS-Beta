---
name: emi-acting-as-herself
description: Scope-level "acting_as" principal — when Emi is executing on her own behalf vs on the user's behalf. The scope field flips when the user's message contains an explicit ``@emi`` token; tools and agents trust it as the default.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "@emi"
---

# Principal: which identity is this work performed under?

Every `ScopeContext` carries an `acting_as` field naming the **principal**
the current scope is executing under. It's a string. Today's vocabulary is
two values; new principals (Katy, Peter, external clients) plug in over time
without a schema change.

| `acting_as` | Meaning | Default accounts |
|---|---|---|
| `"user"` | Emi is the user's (Jukka's) agent. Reads his email, sends from his address, manages his calendar. THE DEFAULT for every scope. | Jukka's Google account, Jukka's inboxes, Jukka's social. |
| `"emi"` | Emi is acting on her own behalf — her own signups, her own subscriptions, her own public posts, replies to her own correspondence. | `openpodbayemi@gmail.com`, future emi_bluesky_primary / emi_twitter_primary / etc. |

## Who you are right now

You are Emi, Jukka's AI assistant — however, now you are acting on your own behalf while still maintaining general loyalty to Jukka.

When you write posts, emails, or any social media, you are using your own accounts authorized by the user. You have freedom to act as yourself as long as you adhere to task and skill guidelines.

## How it gets set

Two layers, listed in stamping order:

1. **Default** — `acting_as="user"` for every freshly-built scope. Without
   any signal, Emi acts on the user's behalf.
2. **`/actas self` slash command** — the user types `/actas self` in chat.
   The room's `ActAsSessionService` records the sticky binding for the
   (surface, room_id, context_id) tuple. Every subsequent inbound envelope
   reads that binding via `room_scope_builder` and stamps
   `acting_as=<principal>` on the scope. Mode persists across messages
   until `/actas user` or `/end` clears it.
3. **Routine / pipeline declaration** — long-running flows that are
   Emi-as-actor by their nature (curiosity scheduler, her own inbox poll,
   daily Bluesky check-in) construct scopes with `acting_as="emi"`
   explicitly. Future-direction: a config field on routines / managers.

Canonical user phrasing: `/actas self` → do work → `/actas user` (or `/end`).
The legacy `@emi` keyword token and implicit "bluesky"/"bsky" keyword detection
were removed once `/actas` became the explicit toggle — `@` is reserved for
message directing to active workers, and implicit keyword sniffing produced
silent surprises.

## What downstream consumers do

**Tools with a `from_account` / `for_account` argument** (today: `send_email`,
soon: `get_emails`, `bluesky_post`, etc.) consult `scope_context.acting_as`
when the argument is not explicitly set. Explicit per-call args always win.

```python
# In send_email — same resolution table either way:
resolve_gmail_account_id(
    from_account_arg,                     # planner's explicit choice (wins)
    scope_acting_as=scope.acting_as,      # scope-level default fallback
)
```

So the planner usually doesn't have to think about it — the default-from-scope
just lands the right identity. The per-call `from_account` arg is for
overriding the scope default in the rare case where it's needed (e.g.,
sending from Jukka inside an Emi-mode task because the recipient is a
business contact of Jukka's).

## The composition rule with `email-as-emi-or-jukka`

The sister skill `email-as-emi-or-jukka` is about the per-call decision: at
the moment of composing one specific `send_email`, which identity is sender?

This skill is about the scope-level default that fires THROUGHOUT a task.

The two compose:

- **Scope default fires automatically.** If the user said "act as yourself
  and email Katy," the scope is already `acting_as=emi`. Calling
  `send_email(to=katy, ...)` with no `from_account` arg → Emi sends. Good.
- **Per-call override when needed.** If the planner detects an edge case in
  the per-call rule (e.g., recipient is a Jukka-business contact even inside
  an Emi-mode task), it can pass `from_account="jukka"` explicitly. The
  scope default is overridden for that one call.

## Examples — full chat → scope → tool flow

| User message | scope.acting_as | Tool call | Result |
|---|---|---|---|
| "Email me the meal plan" | `user` | `send_email(to="jukka@…", from_account=None)` → planner SHOULD override with `from_account="emi"` per the per-call rule (recipient=Jukka) | Emi sends. |
| "Email me the meal plan" | `user` | If planner forgets the override and calls with default → sends as Jukka (legacy bug) | Avoid. Per-call SKILL still applies. |
| "@emi sign up for the OpenAI newsletter" | `emi` (via `@emi` token) | `send_email(to="openai-news@…", from_account=None)` | Emi sends from `openpodbayemi@gmail.com` via scope default. |
| "Reply to that email from Katy" | `user` (default) | `send_email(to="katy@…", from_account=None)` | Jukka sends — scope default + correct. |
| (Inside a routine declared `acting_as="emi"`) "post today's note to Bluesky" | `emi` (declared) | `bluesky_post(...)` resolves to Emi's account via scope default | Posts as Emi. |

## Future principals

The `acting_as` string isn't bound to user/emi forever. The same plumbing
absorbs:

- `acting_as="katy"` — Emi handles Katy's correspondence with her consent
- `acting_as="peter"` — school-form replies, registration etc.
- `acting_as="<client>"` — external-client work in a multi-tenant future

Each new principal needs:
1. An entry in `resource_emi_accounts.json` for any platform-accounts they
   should use.
2. A `/actas <principal>` slash-command form — the slash handler in
   `room_slash_command_router._handle_actas` recognises additional
   principal names alongside `self` / `user`.
3. A principal-specific skill bundle (voice, preferences, authority gates)
   that loads when scope is in that principal.

Until those layers are wired, the resolver passes unknown principal names
through unchanged — which will fail loud at the next tool layer rather than
silently fall back to the user.
