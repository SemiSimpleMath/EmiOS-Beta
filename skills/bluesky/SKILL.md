---
name: bluesky
description: How to read and post on Bluesky via the AT Protocol using http_request. Two-step auth (createSession → accessJwt as Bearer). Auth via auth.bearer pod containing Emi's app password — password never enters your transcript. Use for any "post on bluesky", "check bluesky", "reply on bluesky", "what's happening on bluesky", "bluesky thread" requests.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "bluesky"
      - "bsky"
      - "at protocol"
      - "atproto"
      - "post on"
      - "skeet"
---

# Working with Bluesky via the AT Protocol

Bluesky's API is plain HTTPS over the AT Protocol. There is no Bluesky-specific manager — call `http_request` against `https://bsky.social/xrpc/...` endpoints. This SKILL is the index of what's available and how to authenticate.

## How to format the http_request `body` argument

The `body` argument is **always a string**. For JSON endpoints (every Bluesky XRPC endpoint), pass a **JSON-encoded string** — the tool `json.loads` it at execute time and sends it as JSON. Per-field pod refs inside that JSON still resolve at courier scope (the recursion runs on the parsed structure).

The example bodies shown below are written in pretty-printed JSON for readability. When you actually call `http_request`, emit `body` as the JSON-encoded string equivalent — e.g. for createSession:

```
body: "{\"identifier\":\"<handle>\",\"password\":\"datapod:auth.bearer:<id>/full\"}"
```

## When this skill applies

Any task involving Bluesky: reading the home or author timeline, posting / replying / liking / reposting, searching, looking up profiles. The default assumption: when posting, **`acting_as` should be `emi`** — Bluesky is a public surface and Emi posts as herself unless explicitly told otherwise.

## Authentication — two-step

Bluesky doesn't accept the app password as a Bearer directly. The flow is:

1. **createSession** — POST `https://bsky.social/xrpc/com.atproto.server.createSession` with body `{"identifier": "<emi-handle>", "password": "<app-password-from-pod>"}`. Returns `{accessJwt, refreshJwt, did, handle}`.
2. **Use `accessJwt`** as `Authorization: Bearer <accessJwt>` for every subsequent call.
3. **`accessJwt` lasts ~2 hours**; on `401 ExpiredToken`, either call `refreshSession` with the `refreshJwt` (long-lived) or just re-do createSession from the app password.

For a one-shot post, the simplest pattern is: createSession → post → done. Cache only across calls if the task is multi-step inside a single turn.

## Step 1: createSession

Read the **handle** and **secret ref** for Bluesky from the `available_accounts` block in your prompt (it lists them for the current scope). Use those values verbatim — do not invent pod_ids from environment variable names.

```
POST https://bsky.social/xrpc/com.atproto.server.createSession
Content-Type: application/json
Body: {
  "identifier": "<paste handle from available_accounts>",
  "password":   "<paste secret ref from available_accounts (the datapod:... string ending in /full)>"
}
```

The `/full` projection resolves under courier scope automatically (http_request handles it), so the actual password never appears in your transcript.

Response:
```json
{
  "accessJwt": "eyJ...",
  "refreshJwt": "eyJ...",
  "handle": "openpodbayemi.bsky.social",
  "did": "did:plc:..."
}
```

## Step 2: Use accessJwt as Bearer for any subsequent call

```
GET https://bsky.social/xrpc/app.bsky.feed.getTimeline
Authorization: Bearer <accessJwt>
```

Pass the JWT inline in the next http_request call's headers. (For multi-step tasks the planner can cache it in agent_input across calls; for one-shots, just re-call createSession.)

## Common operations

### Post a top-level skeet

```
POST https://bsky.social/xrpc/com.atproto.repo.createRecord
Authorization: Bearer <accessJwt>
Content-Type: application/json
Body: {
  "repo": "<did from createSession>",
  "collection": "app.bsky.feed.post",
  "record": {
    "$type": "app.bsky.feed.post",
    "text": "Hello from Emi.",
    "createdAt": "<ISO-8601 UTC, e.g. 2026-05-23T20:00:00.000Z>"
  }
}
```

The `text` field is limited to **300 characters (graphemes)**. Newlines are literal `\n`.

### Reply to a post

Same `createRecord` call, but include a `reply` field in the record:

```json
"record": {
  "$type": "app.bsky.feed.post",
  "text": "Replying.",
  "createdAt": "<ISO>",
  "reply": {
    "root":   {"uri": "at://...", "cid": "..."},
    "parent": {"uri": "at://...", "cid": "..."}
  }
}
```

Fetch parent's `uri` + `cid` via `app.bsky.feed.getPostThread` or store them when reading the timeline.

### Read the home timeline

```
GET https://bsky.social/xrpc/app.bsky.feed.getTimeline?limit=20
Authorization: Bearer <accessJwt>
```

Returns `{feed: [{post: {uri, cid, author: {handle, displayName}, record: {text, createdAt, ...}, replyCount, likeCount, repostCount}}]}`.

### Read a specific author's timeline

```
GET https://bsky.social/xrpc/app.bsky.feed.getAuthorFeed?actor=<handle-or-did>&limit=20
Authorization: Bearer <accessJwt>
```

### Like a post

```
POST https://bsky.social/xrpc/com.atproto.repo.createRecord
Authorization: Bearer <accessJwt>
Body: {
  "repo": "<emi's did>",
  "collection": "app.bsky.feed.like",
  "record": {
    "$type": "app.bsky.feed.like",
    "subject": {"uri": "<post uri>", "cid": "<post cid>"},
    "createdAt": "<ISO>"
  }
}
```

### Repost

Same as `like`, but `collection = "app.bsky.feed.repost"`.

### Search posts

```
GET https://bsky.social/xrpc/app.bsky.feed.searchPosts?q=<query>&limit=25
Authorization: Bearer <accessJwt>
```

### Lookup a profile

```
GET https://bsky.social/xrpc/app.bsky.actor.getProfile?actor=<handle-or-did>
Authorization: Bearer <accessJwt>
```

## Identity discipline

Bluesky is a public surface. Posts are permanent (technically retractable, but the network has likely already replicated them). Authority rule:

- **Reads** (timeline, profile, search) — fine at AUTH_USER, no extra approval.
- **Writes** (post, reply, like, repost, follow) — should always go through user approval gate before sending, especially for top-level posts and replies that mention third parties. Use the same ticket-approval pattern as send_email tool approval. Even when `acting_as=emi` and the scope-level intent is clear, double-check before posting.

## Rate limits

Bluesky's documented limits are generous (~3000 req/5min per app password). Stay well under for safety. Don't burst-post; if posting multiple items, throttle to one per 5–10 seconds.

## Error handling

- **401 ExpiredToken** — accessJwt aged out (>~2hr). Re-call createSession or use refreshSession.
- **400 InvalidRequest** — schema error in your body. Common causes: text >300 chars, missing `$type`, malformed `createdAt`.
- **429 RateLimitExceeded** — back off; honor `Retry-After` header.
- **400 InvalidIdentifier** — wrong handle or DID. Re-resolve via `app.bsky.actor.getProfile`.

## Identity defaults

When the planner needs Emi's handle, use `emi_accounts.get_emi_handle("bluesky")`. When the planner needs to know which auth pod to use, resolve via `resource_emi_accounts.json` (platform=bluesky → `auth.pod_id` field, env-resolved).

Future principals (Katy, Peter) with their own Bluesky accounts would plug in as new entries in `resource_emi_accounts.json` and their own `auth.bearer` pods. The same SKILL applies regardless of which principal — only the handle + pod_id differ.
