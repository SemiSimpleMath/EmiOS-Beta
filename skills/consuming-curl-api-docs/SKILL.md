---
name: consuming-curl-api-docs
description: How to read API documentation written as curl examples and use the documented endpoints via http_request. Most public APIs (Moltbook, Discord, Stripe, GitHub, Notion, OpenAI, etc.) document themselves with curl commands; this skill is the translation table from curl flags to http_request arguments, with pod-aware auth handling.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "curl"
      - "API docs"
      - "API documentation"
      - "developer docs"
      - "sign up for"
      - "create an account"
      - "use the API"
      - "API endpoint"
      - "REST API"
---

# Reading curl-shaped API documentation

You've encountered API documentation written as `curl` commands. Almost every public-facing REST API on the internet documents itself this way because curl is the universal lowest-common-denominator HTTP CLI. **You do NOT need to run curl** — `http_request` does everything curl does, more safely and with pod-aware auth. This skill is the translation table.

## The mental model

curl is "HTTP from the shell"; `http_request` is "HTTP from inside Emi." Same wire effect. Translation is mechanical.

| curl flag | http_request argument |
|---|---|
| `<url>` (positional) | `url=<url>` |
| `-X METHOD`, `--request METHOD` | `method="METHOD"` (defaults to GET) |
| `-H "Name: value"` (one per header) | `headers={"Name": "value"}` (collect them all into one dict) |
| `-d '<body>'`, `--data '<body>'` | `body=<body>` (if string is JSON, you can also pass a dict directly) |
| `--data-raw`, `--data-binary` | Same as `-d` for our purposes |
| `--data-urlencode 'k=v'` | Treat as `body={"k": "v"}` with `Content-Type: application/x-www-form-urlencoded` |
| `-F 'file=@path/to/file'` (multipart) | Mint a pod from the file via `mint_pod_from_path`, pass `body=<pod_id>`. (Multipart with multiple parts: out of v1 scope — fall back to constructing the request manually) |
| `-G` (force GET with query string) | `method="GET"`, put params in `query_params={...}` instead of `body` |
| `-u user:pass` (basic auth) | Set `headers={"Authorization": "Basic <base64(user:pass)>"}` — and put `user:pass` in an `auth.bearer` pod first |
| `--cookie 'k=v'`, `-b 'k=v'` | `headers={"Cookie": "k=v"}` |
| `-A 'agent'` | `headers={"User-Agent": "agent"}` |
| `-L`, `--location` (follow redirects) | `follow_redirects=True` (default already true) |
| `--max-time N` | `timeout_s=N` |
| `-o file.json`, `-s`, `-v`, `-i`, etc. | Output-control flags — ignore. `http_request` returns structured output. |

## Authentication translation (the important part)

When you see a curl command with auth, **never inline the token**. The token should go into an `auth.bearer` (or `auth.oauth`) pod, and your http_request call references the pod by URI.

### Bearer token

Docs say:
```bash
curl https://api.example.com/me \
     -H "Authorization: Bearer ABC123XYZ"
```

You write:
```python
http_request(
    url="https://api.example.com/me",
    headers={"Authorization": "Bearer datapod:auth.bearer:<pod_id>/full"},
)
```

The string `datapod:auth.bearer:<pod_id>/full` is a pod reference. `http_request` resolves it at courier scope, substitutes the actual token at execute time, and fires the call. The token never enters your transcript.

### API key in custom header

Docs say:
```bash
curl https://api.example.com/data \
     -H "X-API-Key: SECRET_KEY_VALUE"
```

You write:
```python
http_request(
    url="https://api.example.com/data",
    headers={"X-API-Key": "datapod:auth.bearer:<pod_id>/full"},
)
```

Same pattern — only the header name changes; the pod reference goes in the value.

### Basic auth

Docs say:
```bash
curl -u "username:password" https://api.example.com/secure
```

Mint an `auth.bearer` pod containing the base64-encoded `username:password`, then:
```python
http_request(
    url="https://api.example.com/secure",
    headers={"Authorization": "Basic datapod:auth.bearer:<pod_id>/full"},
)
```

### OAuth

Docs say (after a separate OAuth setup):
```bash
curl https://api.example.com/me \
     -H "Authorization: Bearer $ACCESS_TOKEN"
```

If the token rotates (most OAuth flows), use an `auth.oauth` pod instead of `auth.bearer`. The pod stores access + refresh; before any call, run `oauth_token_refresh(pod_id)` if the token is near expiry, then call http_request with `Authorization: Bearer datapod:auth.oauth:<pod_id>/access_token`.

## Worked examples

### Example 1 — Simple GET

Docs say:
```bash
curl https://api.github.com/users/torvalds
```

You write:
```python
http_request(
    url="https://api.github.com/users/torvalds",
    headers={"Accept": "application/vnd.github+json"},
)
```

(The `Accept` header isn't strictly required for github but the docs recommend it; this is just polishing.)

### Example 2 — POST with JSON body

Docs say:
```bash
curl -X POST https://api.moltbook.example/v1/accounts \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"handle": "jukka", "bio": "personal AI builder"}'
```

You write:
```python
http_request(
    url="https://api.moltbook.example/v1/accounts",
    method="POST",
    headers={
        "Authorization": "Bearer datapod:auth.bearer:<pod_id>/full",
        "Content-Type": "application/json",
    },
    body={"handle": "jukka", "bio": "personal AI builder"},
)
```

`http_request` automatically serializes a dict body to JSON; you do not need to wrap it in quotes.

### Example 3 — POST with form-encoded body

Docs say:
```bash
curl -X POST https://accounts.spotify.com/api/token \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "grant_type=client_credentials&client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET"
```

You write:
```python
http_request(
    url="https://accounts.spotify.com/api/token",
    method="POST",
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    body="grant_type=client_credentials"
         f"&client_id={datapod:auth.bearer:<pod_client_id>/full}"
         f"&client_secret={datapod:auth.bearer:<pod_secret>/full}",
)
```

Hmm — body is a plain string here, but the pod refs need to be SUBSTITUTED. `http_request` only resolves pod refs in `headers` values or as a WHOLE body in v1, not inside an embedded string. For form-encoded auth, build a separate pod containing the entire encoded body string and pass `body="datapod:auth.bearer:<pod_full_body>/full"`. Or use the cleaner OAuth flow: put client_id+secret in env vars used by an `auth.oauth` pod's materializer; `oauth_token_refresh` handles the refresh dance internally.

### Example 4 — PATCH with selective fields

Docs say:
```bash
curl -X PATCH https://api.notion.example/v1/pages/abc123 \
     -H "Authorization: Bearer $TOKEN" \
     -H "Notion-Version: 2022-06-28" \
     -d '{"properties": {"Status": {"select": {"name": "Done"}}}}'
```

You write:
```python
http_request(
    url="https://api.notion.example/v1/pages/abc123",
    method="PATCH",
    headers={
        "Authorization": "Bearer datapod:auth.bearer:<pod_id>/full",
        "Notion-Version": "2022-06-28",
    },
    body={"properties": {"Status": {"select": {"name": "Done"}}}},
)
```

### Example 5 — File upload via multipart

Docs say:
```bash
curl -X POST https://api.example.com/upload \
     -H "Authorization: Bearer $TOKEN" \
     -F "file=@/Users/jukka/receipt.jpg" \
     -F "category=expense"
```

For single-file uploads:
```python
# 1. Mint a pod from the local file (bash_manager surface).
photo_pod_id = mint_pod_from_path(path="~/Pictures/receipt.jpg")

# 2. Fire http_request with body as the file pod.
http_request(
    url="https://api.example.com/upload",
    method="POST",
    headers={"Authorization": "Bearer datapod:auth.bearer:<pod_id>/full"},
    body=photo_pod_id,  # bytes from pod
)
```

For multi-part with multiple fields plus the file: v1.1 doesn't support assembling multipart from multiple pods in one call. The workaround is to construct the multipart body as raw bytes in a single pod and post it with the `Content-Type: multipart/form-data; boundary=...` header. If you find yourself doing this often, surface it as a request for a v1.2 multipart helper.

### Example 6 — Sensitive response (sealed)

Docs say:
```bash
curl https://api.bank.example/v1/balance \
     -H "Authorization: Bearer $TOKEN"
```

If the response contains data you should NOT see (account number, raw balance, transaction list), set `response_pod_kind` to seal it. A downstream skill at higher authority reads the pod and emits only summary information back to chat.

```python
result = http_request(
    url="https://api.bank.example/v1/balance",
    headers={"Authorization": "Bearer datapod:auth.bearer:<pod_id>/full"},
    response_pod_kind="financial.private",
)
# result.data["response_pod_id"] = the new pod; body is NOT in result.
# A financial-authority skill can pod_fetch it to compute summary stats.
```

## When NOT to translate

- The doc example uses curl as a smoke test, not the real integration. (Often you'll find a Python/Node SDK code block alongside the curl one — prefer the SDK example only if Emi has the SDK installed; otherwise translate the curl.)
- The endpoint requires streaming or websocket — `http_request` is request/response only. Reach for `playwright_manager` or surface as a request for a streaming HTTP tool.
- The endpoint is browser-only (CORS-locked, no public API) — use `playwright_manager` instead.

## Anti-patterns

1. **Never paste a token directly into a header value.** Even if the docs literally show `-H "Authorization: Bearer abc123"`, your http_request call should reference an `auth.bearer` pod for the value. The token must live in an env var and be pod-resolved at courier scope.
2. **Never `pod_fetch` the token and concatenate it into a body string.** The pod system enforces that secrets stay out of LLM context; concatenating them into a string you build defeats the system.
3. **Never construct a shell-quoted curl command and try to run it via bash_manager.** Even if Emi had curl available, it would be strictly worse: shell escaping is error-prone, tokens land in `/proc/<pid>/cmdline` and shell history, and the result is unstructured stdout. http_request does the same thing safely.
4. **Don't `find_tool` for "curl" and try to use what comes back.** There is no curl tool in Emi. The right primitive is `http_request`. This skill is the translation.

## When the docs are sparse

If you only see one curl example for an API and need to call other endpoints not in the doc:
- The same auth pattern (same header name, same pod) applies to every endpoint on the API
- Method and path change per endpoint
- Bodies for write endpoints usually follow the same shape as response bodies for read endpoints — you can infer the schema

If completely stuck, call `http_request` with a `OPTIONS` request to the base URL and see what the server returns; many APIs document themselves via OpenAPI at `/openapi.json` or `/.well-known/openapi.json`.

## Related skills

- `github` — concrete example of this pattern, fully worked out for GitHub's API
- `extending-emi-materializers` — how to add a new pod kind (e.g. if an API uses a non-bearer auth scheme that needs custom projections)
