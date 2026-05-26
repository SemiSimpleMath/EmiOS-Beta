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

**CRITICAL: always pass `seal_fields: ["accessJwt", "refreshJwt"]` on this call.** Bluesky returns JWTs in the response that the next call relays in `Authorization: Bearer …`. If you transcribe the JWT from the response body into the next call's header yourself, your LLM will silently corrupt it (long opaque base64 strings get one or two characters flipped during token prediction — same failure mode that motivates the password-pod pattern, just on the inbound side). With `seal_fields`, the JWTs land in their own pods, the response body has `datapod:auth.session:<id>/full` references where the JWTs would have been, and you relay those references verbatim in the next call — the courier resolver substitutes the real bytes at execute time.

```
http_request:
  url:    https://bsky.social/xrpc/com.atproto.server.createSession
  method: POST
  headers: {"Content-Type": "application/json"}
  body:   "{\"identifier\": \"<handle from available_accounts>\", \"password\": \"<secret ref from available_accounts>\"}"
  seal_fields: ["accessJwt", "refreshJwt"]
```

Response you'll see (the JWTs are replaced with pod-refs; the rest is verbatim):
```json
{
  "did": "did:plc:o4gn7k7srpqbpdo4xv6qomb2",
  "didDoc": {"service": [{"serviceEndpoint": "https://stropharia.us-west.host.bsky.network"}, ...]},
  "handle": "openpodbayemi.bsky.social",
  "accessJwt": "datapod:auth.session:<uuid-a>/full",
  "refreshJwt": "datapod:auth.session:<uuid-b>/full",
  "active": true
}
```

The `did` and `serviceEndpoint` are still visible — you need them for subsequent calls.

## Step 2: Use the accessJwt pod-ref as Bearer for every subsequent call

```
http_request:
  url:    <serviceEndpoint>/xrpc/app.bsky.feed.getTimeline
  method: GET
  headers:
    Authorization: "Bearer datapod:auth.session:<uuid-a>/full"   ← copy verbatim from createSession response
  query_params: {"limit": "20"}
```

`accessJwt` lasts ~2 hours; on `401 ExpiredToken`, either call `refreshSession` (passing the `refreshJwt` pod-ref the same way) or just re-do createSession from the app password.

For a one-shot post, the simplest pattern is: createSession → post → done. The pod-refs work across multiple http_request calls in the same task.

## Common operations

In every example below, `<accessJwt>` means the pod-ref string you got from createSession's response (`datapod:auth.session:<uuid>/full`) — relay it verbatim in the `Authorization: Bearer …` header; do NOT transcribe a raw JWT from anywhere.

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

Bluesky is a public surface. Posts are permanent (technically retractable, but the network has likely already replicated them).

- **Reads** (timeline, profile, search) — fine at AUTH_USER.
- **Writes** (post, reply, like, repost, follow) — Emi acts directly. Do NOT route through ask_user / ticket approval for routine writes; the whole point of an autonomous agent is that the user shouldn't have to babysit. The judgment layer is the auto-injected safety/engagement/voice skills (`emi-bluesky-safety`, `emi-bluesky-engagement`, `emi-bluesky-voice`) — follow them and post. The user can delete or correct after the fact if needed.

The only writes that warrant a pre-send ticket are the genuinely high-stakes cases the safety skill flags (e.g. a top-level post calling out a specific person by handle, or anything that crosses one of `emi-bluesky-safety`'s hard rules). If the safety skill says go, go.

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
