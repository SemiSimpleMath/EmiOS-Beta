---
name: github
description: How to query and interact with GitHub via api.github.com using the http_request tool. Covers user info, repos, pull requests, issues, releases, commits, search. Authentication via auth.bearer pod containing a Personal Access Token — token never enters your transcript. Use for any "check my github", "what PRs", "what issues", "github stars", "latest release" requests.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "github"
      - "PR"
      - "pull request"
      - "open issues"
      - "my repos"
      - "github stars"
      - "github stats"
      - "latest release"
      - "release notes"
      - "what's the latest on"
      - "gh repo"
---

# Working with GitHub via the REST API

Emi has no dedicated GitHub manager — GitHub's REST API is just HTTP, so you call `http_request` against `https://api.github.com/...` endpoints. This skill is the index of what's available and how to authenticate.

## When this skill applies

Any task involving GitHub content: user profiles, repository data, pull requests, issues, releases, commits, contributors, search. Includes "my github" / "my repos" / "my PRs" requests once you've resolved the user's GitHub handle.

## Authentication

### Path A — Public reads, no auth needed

Most public endpoints work without authentication, rate-limited to **60 req/hr per IP**:

- `GET /users/<user>` — public profile
- `GET /users/<user>/repos` — list public repos
- `GET /repos/<owner>/<repo>` — repo metadata
- `GET /repos/<owner>/<repo>/pulls?state=open` — open PRs on public repos
- `GET /search/repositories?q=...`

Just call `http_request` with the URL. No headers needed beyond `Accept: application/vnd.github+json`.

### Path B — Authenticated via auth.bearer pod

For private repos, your own user data, write operations, or the higher rate limit (5000 req/hr), use a GitHub Personal Access Token stashed in an `auth.bearer` pod.

**One-time setup:**
1. Generate a PAT at https://github.com/settings/tokens (fine-grained or classic; fine-grained preferred — scope it to read-only or specific repos)
2. Set the env var: `EMI_POD_GITHUB_PAT=ghp_...`
3. Mint the pod once (any agent at AUTH_USER scope can do this):

```python
PodStore().put_secret_pod(
    pod_type="auth.bearer",
    owner_subject_id="jukka",
    name="GitHub PAT",
    env_ref="EMI_POD_GITHUB_PAT",
    scope=scope,
)
```

The returned `pod_id` is what you reference. From then on:

```python
http_request(
    url="https://api.github.com/user",
    headers={
        "Authorization": "Bearer datapod:auth.bearer:<pod_id>/full",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
    expect_status=[200],
)
```

The token never enters your transcript. The `http_request` tool elevates to courier scope, substitutes the real Bearer value, fires the request, and reports back only `status / content_type / content_length / body_json`.

## The common endpoints

### User info
- `GET /user` — authenticated user (the PAT owner)
- `GET /users/<user>` — any public user's profile

### Repos
- `GET /users/<user>/repos?type=owner&sort=updated&per_page=30`
- `GET /repos/<owner>/<repo>` — single repo
- `GET /repos/<owner>/<repo>/commits?since=<ISO>&per_page=20`
- `GET /repos/<owner>/<repo>/contributors`
- `GET /repos/<owner>/<repo>/languages` — language byte counts
- `GET /repos/<owner>/<repo>/stats/participation` — weekly commit counts

### Pull requests
- `GET /repos/<owner>/<repo>/pulls?state=open&sort=updated&direction=desc`
- `GET /repos/<owner>/<repo>/pulls/<number>` — single PR
- `GET /repos/<owner>/<repo>/pulls/<number>/files`
- `GET /repos/<owner>/<repo>/pulls/<number>/reviews`
- `GET /repos/<owner>/<repo>/pulls/<number>/commits`

### Issues (note: PRs are returned as issues by default; filter on `pull_request` field)
- `GET /repos/<owner>/<repo>/issues?state=open&sort=updated&direction=desc`
- `GET /repos/<owner>/<repo>/issues/<number>`
- `GET /repos/<owner>/<repo>/issues/<number>/comments?since=<ISO>`

### Releases
- `GET /repos/<owner>/<repo>/releases?per_page=10`
- `GET /repos/<owner>/<repo>/releases/latest`
- `GET /repos/<owner>/<repo>/releases/tags/<tag>`

### Search
- `GET /search/repositories?q=<query>+stars:>100&sort=stars`
- `GET /search/issues?q=<query>+author:<user>+state:open`
- `GET /search/issues?q=is:pr+author:<user>+is:open` — your open PRs across all repos
- `GET /search/code?q=<query>+repo:<owner>/<repo>`
- `GET /search/commits?q=<query>`

Search results return up to 1000 items max; use specific filters.

### Activity feed
- `GET /user/events` (authenticated) — your event stream (push, PR, issue, etc.)
- `GET /users/<user>/events/public` — public events for a user
- `GET /repos/<owner>/<repo>/events` — events on a repo

## Worked examples

### "Check my GitHub stats"

```python
# 1. Resolve "my" username from KG / user_bio_context — don't guess.
#    Suppose it resolves to "jukka-virtanen".
# 2. Fetch the public profile.
http_request(
    url="https://api.github.com/users/jukka-virtanen",
    headers={"Accept": "application/vnd.github+json"},
)
# Returns: followers, following, public_repos, public_gists, created_at, ...

# 3. Recent repos:
http_request(
    url="https://api.github.com/users/jukka-virtanen/repos",
    query_params={"sort": "updated", "per_page": "5"},
    headers={"Accept": "application/vnd.github+json"},
)
```

Render a summary: "X public repos, Y followers, recently active on [repo names]."

### "What PRs are open on anthropics/anthropic-sdk-python?"

```python
http_request(
    url="https://api.github.com/repos/anthropics/anthropic-sdk-python/pulls",
    query_params={"state": "open", "sort": "updated", "direction": "desc", "per_page": "20"},
    headers={"Accept": "application/vnd.github+json"},
)
```

For each PR in `body_json`, render: `#<number> — <title> by @<user.login>` and `created_at` as relative time.

### "Did anyone comment on my open issues today?"

```python
# 1. Get authenticated user identity.
http_request(
    url="https://api.github.com/user",
    headers={"Authorization": "Bearer datapod:auth.bearer:<pod>/full",
             "Accept": "application/vnd.github+json"},
    expect_status=[200],
)
# body_json.login -> "jukka-virtanen"

# 2. Find your open issues across repos:
http_request(
    url="https://api.github.com/search/issues",
    query_params={"q": "author:jukka-virtanen state:open is:issue"},
    headers={"Authorization": "Bearer datapod:auth.bearer:<pod>/full",
             "Accept": "application/vnd.github+json"},
)
# body_json.items[] -> issue list

# 3. For each issue, fetch recent comments (since today's start in UTC).
for issue in body_json["items"]:
    http_request(
        url=issue["comments_url"],
        query_params={"since": "2026-05-19T00:00:00Z"},
        headers={"Authorization": "Bearer datapod:auth.bearer:<pod>/full",
                 "Accept": "application/vnd.github+json"},
    )
```

Summarize what's new.

### "What's the latest release of vscode?"

```python
http_request(
    url="https://api.github.com/repos/microsoft/vscode/releases/latest",
    headers={"Accept": "application/vnd.github+json"},
)
```

Returns `name`, `tag_name`, `published_at`, `body` (release notes markdown).

### "Find Python repos about agent frameworks, sorted by stars"

```python
http_request(
    url="https://api.github.com/search/repositories",
    query_params={
        "q": "agent framework language:python stars:>500",
        "sort": "stars", "order": "desc", "per_page": "20",
    },
    headers={"Accept": "application/vnd.github+json"},
)
```

## Pagination

GitHub uses RFC 5988 `Link` headers (`<...>; rel="next"`). For most personal-assistant queries, set `per_page=100` (the max) and filter via the query rather than walking pages. The `http_request` tool surfaces the response `Link` header in `data.headers["Link"]` if you need it.

## Rate limit awareness

Every authenticated response includes:
- `X-RateLimit-Limit` (5000 for authenticated, 60 unauthenticated)
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset` (Unix epoch seconds)

If remaining drops below 10, pause; if reset is soon (< 60s), wait and retry. For repeated batch work, prefer GraphQL (`/graphql`) which has a separate rate-limit pool counted by computed point cost.

## Anti-patterns

- **Don't `pod_fetch` the PAT and pass it as a literal Authorization header.** That leaks the token into your transcript. Always use the `datapod:auth.bearer:<id>/full` reference inside the header value — courier substitution handles it.
- **Don't scrape github.com when an API endpoint exists.** Scraping is fragile (HTML changes), slower, and harder to rate-limit. Always check if an API endpoint exists first.
- **Don't paginate the entire result set when you only need recent activity.** Use `since=<ISO>` filters, `per_page=100`, or stop after the first page when the data window is bounded.
- **Don't hardcode "my username".** Resolve it from KG / user_bio_context, or call `/user` with the PAT.

## When to use a different tool

- **Local git operations** (`git log`, `git status` on a local clone) → `bash_manager` with git commands
- **Trending page, README rendering, the repo's web UI** → no API equivalent; use `scrape_url` for HTML pages or `playwright_manager` for JS-heavy views
- **Authenticated write actions in a local checkout** (push, pull, commit) → `bash_manager` with local git
- **Multi-step UI flows** (clicking through web UI, dragging in projects/boards) → `playwright_manager`

## What's not covered here

This skill is read-focused. Write operations (creating issues/PRs, commenting, releases) are also just HTTP — use `method="POST"` or `"PATCH"` with the same authentication pattern. The endpoints follow the same shape; consult https://docs.github.com/en/rest for the body schemas.

## Why this isn't a dedicated tool

A GitHub-specific tool would duplicate what `http_request` already does. The pod-aware auth, the audit trail, the size caps, the error model — all already in `http_request`. Layering GitHub-specific behavior on top would be a wrapper around a wrapper. By keeping it as a skill, every new GitHub endpoint is "add a section to this file," not "ship a new tool."

This is the pattern for every API integration: `http_request` + an `auth.bearer` pod + a SKILL.md. Expect the same shape for Spotify, WHOOP, Reddit, X, Stripe, etc.
