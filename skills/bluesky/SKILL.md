---
name: bluesky
description: How to read and act on Bluesky. The common verbs — read the timeline, open a post, post, reply, like — are dedicated ref-anchored tools (bluesky_timeline / bluesky_hydrate_post / bluesky_post / bluesky_reply / bluesky_like) that handle auth for you. Long-tail operations (repost, search, profiles, follow) go through http_request against AT Protocol XRPC endpoints. Use for any "post on bluesky", "check bluesky", "reply on bluesky", "what's happening on bluesky", "bluesky thread" request.
license: Apache-2.0
metadata:
  author: emi-team
  version: "2.0"
  auto_inject_when:
    task_keywords:
      - "bluesky"
      - "bsky"
      - "at protocol"
      - "atproto"
      - "post on"
      - "skeet"
---

# Working with Bluesky

For the everyday verbs, use the **dedicated Bluesky tools**. They handle authentication internally (you never call createSession or touch a password), and — critically — actions are **anchored to a post ref**, so you never copy a post URL and a reply can never land on the wrong post.

The default identity: Bluesky is a public surface and **the assistant posts as itself** unless explicitly told otherwise.

## The normal flow — dedicated tools

1. **`bluesky_timeline`** — fetch the home timeline. Returns a compact numbered list:
   ```
   [b1] @werd.io · "Well, that was the first time I finished an open source event by dancing to Dancing Queen…"
   [b2] @werd.io · "Such a big deal. I was excited to be in the room…"  [image: "A graphic announcing the Open Social Awards…"; quotes: "Winners of the first ever…"]
   ```
   Each post gets a stable ref (`b1`, `b2`, …). Call this **first** before replying to or liking anything.

2. **`bluesky_hydrate_post post_ref=b2`** — open ONE post on its own. If it has an image, the **actual image is shown to you** (not just alt text). **Do this before replying to any post that has an image or quotes another post** — react to what is actually there, don't guess what an image shows. (This is the exact failure that motivated these tools: a post's image/quote was guessed at instead of read.)

3. **Act, by ref:**
   - **`bluesky_post text="…"`** — publish an original top-level post.
   - **`bluesky_reply post_ref=b1 text="…"`** — reply to a specific post. Pass the ref of the post your text is actually about; the reply binds to that ref and cannot drift to a sibling post.
   - **`bluesky_like post_ref=b1`** — like a specific post.

Text limit is **300 characters**. The reply/like tools echo back which post they hit — glance at that echo to confirm you targeted the right one.

### Picking the right ref
When several posts are by the same author (or look similar), the ref is the contract: select the ref whose **text** matches what your reply is about. If a post has an image or a quote marker and your reply depends on what's in it, hydrate it first. Never reconstruct a post URI by hand — there is no field for that in these tools, by design.

## Identity discipline

Bluesky is public and posts are effectively permanent (technically retractable, but the network has likely already replicated them).

- **Reads** (timeline, hydrate, search, profile) — fine at AUTH_USER.
- **Writes** (post, reply, like, repost, follow) — **the assistant acts directly.** Do NOT route routine writes through ask_user / ticket approval — the point of an autonomous agent is that the user shouldn't have to babysit. The judgment layer is the auto-injected safety/engagement/voice skills (`emi-bluesky-safety`, `emi-bluesky-engagement`, `emi-bluesky-voice`) — follow them and post. The user can delete or correct after the fact.

The only writes that warrant a pre-send ticket are the genuinely high-stakes cases `emi-bluesky-safety` flags (e.g. a top-level post calling out a specific person by handle, or anything crossing one of its hard rules). If the safety skill says go, go.

## Rate limits

Bluesky's limits are generous (~3000 req / 5 min). Don't burst-post; if posting multiple items, throttle to one per 5–10 seconds.

---

## Long-tail operations — raw `http_request`

The dedicated tools cover timeline / hydrate / post / reply / like. For **repost, search, profile lookup, author feeds, follow**, there's no dedicated tool — call `http_request` against the AT Protocol XRPC endpoints. These need explicit auth.

### Authentication for raw calls (two-step)

Bluesky doesn't accept the app password as a Bearer directly:

1. **createSession** — POST `https://bsky.social/xrpc/com.atproto.server.createSession`. Read the **handle** and **secret ref** from the `available_accounts` / `resource_accounts` block in your prompt (it lists them for the current scope) — use them verbatim, don't invent pod_ids from env-var names.

   **Always pass `seal_fields: ["accessJwt", "refreshJwt"]`.** Bluesky returns JWTs the next call must relay in `Authorization: Bearer …`. If you transcribe a JWT yourself, your LLM silently corrupts one or two characters of the opaque base64 and the call fails. With `seal_fields`, the JWTs land in their own pods and the response shows `datapod:auth.session:<id>/full` references — relay those verbatim; the courier resolver substitutes the real bytes at execute time.

   ```
   http_request:
     url:    https://bsky.social/xrpc/com.atproto.server.createSession
     method: POST
     headers: {"Content-Type": "application/json"}
     body:   "{\"identifier\": \"<handle from available_accounts>\", \"password\": \"<secret ref from available_accounts>\"}"
     seal_fields: ["accessJwt", "refreshJwt"]
   ```

   The response keeps `did` and `didDoc.service[].serviceEndpoint` visible — you need the serviceEndpoint (the user's PDS) as the base URL for subsequent calls, and `did` as `repo`.

2. **Use the `accessJwt` pod-ref** as `Authorization: Bearer datapod:auth.session:<uuid>/full` (relay verbatim) for every subsequent call. It lasts ~2h; on `401 ExpiredToken`, re-call createSession.

The `body` argument is **always a JSON-encoded string** — the tool `json.loads` it at execute time; per-field pod-refs inside still resolve at courier scope.

### Repost
```
POST <serviceEndpoint>/xrpc/com.atproto.repo.createRecord
Authorization: Bearer <accessJwt pod-ref>
Body: {"repo":"<did>","collection":"app.bsky.feed.repost","record":{"$type":"app.bsky.feed.repost","subject":{"uri":"<post uri>","cid":"<post cid>"},"createdAt":"<ISO>"}}
```
(Get the target `uri` + `cid` from a prior `bluesky_timeline` ref via `bluesky_hydrate_post`, or from `getPostThread`.)

### Search posts
```
GET https://bsky.social/xrpc/app.bsky.feed.searchPosts?q=<query>&limit=25
Authorization: Bearer <accessJwt pod-ref>
```

### Author feed
```
GET <serviceEndpoint>/xrpc/app.bsky.feed.getAuthorFeed?actor=<handle-or-did>&limit=20
Authorization: Bearer <accessJwt pod-ref>
```

### Lookup a profile
```
GET https://bsky.social/xrpc/app.bsky.actor.getProfile?actor=<handle-or-did>
Authorization: Bearer <accessJwt pod-ref>
```

### Follow
```
POST <serviceEndpoint>/xrpc/com.atproto.repo.createRecord
Authorization: Bearer <accessJwt pod-ref>
Body: {"repo":"<did>","collection":"app.bsky.graph.follow","record":{"$type":"app.bsky.graph.follow","subject":"<did-to-follow>","createdAt":"<ISO>"}}
```

### Error handling (raw calls)
- **401 ExpiredToken** — accessJwt aged out; re-call createSession.
- **400 InvalidRequest** — body schema error (text >300 chars, missing `$type`, malformed `createdAt`).
- **429 RateLimitExceeded** — back off; honor `Retry-After`.
- **400 InvalidIdentifier** — wrong handle/DID; re-resolve via `app.bsky.actor.getProfile`.

## Multiple principals

The dedicated tools resolve the acting principal's Bluesky account internally (handle + app-password pod). Future principals with their own Bluesky accounts plug in as new account entries in the env registry plus their own `auth.bearer` pod — the same tools and this skill apply; only the account differs.
