---
name: reddit
description: How to read and act on Reddit through the BROWSER (Playwright), as the assistant's own logged-in account, only in acting_as self mode. Reddit is driven like a human in a real browser — never the API, never the scraping tools — so the account isn't flagged. Read the feed, open a post, upvote, comment, submit. Use for any "post on reddit", "check reddit", "reply on reddit", "what's on r/...", "reddit" request when acting as herself.
license: Apache-2.0
metadata:
  author: emi-team
  version: "2.0"
  auto_inject_when:
    task_keywords:
      - "reddit"
      - "subreddit"
      - "r/"
      - "upvote"
      - "karma"
    requires_scope_acting_as: self
---

# Driving Reddit (browser, as herself)

Reddit is driven through the **real browser** (the Playwright path), logged in as her own account, and **only in `acting_as: self` mode**. This is deliberate: Reddit treats API and scraped access as abuse and flags accounts for it, so she behaves like a normal logged-in human in a real session. **Never use the Reddit API, never use the scraping tools** (`scrape_url` etc. are blocked for Reddit on purpose). `web-interaction` (human pacing, clearing overlays, never fighting a CAPTCHA) and `emi-reddit-safety` (the authoritative account-protection rules) apply on top of this.

She is **already logged in** — the browser profile holds her session. Don't log in again; if you ever land logged-out, stop and surface it. A fresh login from automation is itself a flag.

## Reading
- Go to `https://www.reddit.com` (home feed) or `https://www.reddit.com/r/<sub>`. Scroll to load more posts.
- To read a post and its comments, click into the post's title, then use `web_get_content` for the text — the snapshot only shows interactive elements (arrows, links, boxes), not the body or comment text.
- Before acting in a subreddit she hasn't used, read its **rules** (the sidebar, or `https://www.reddit.com/r/<sub>/about/rules`). Subreddit rules and `emi-reddit-safety` win over a task request — if a sub forbids bots, don't post there.

## Acting — one at a time, human-paced
- **Upvote**: the vote arrows show in the snapshot as **labeled refs** (`[button] Upvote` / `Downvote`) — one `[Upvote, Downvote, (Award), Share]` row per post, in feed order — so click the target post's `Upvote` by **ref** (`web_click_ref_snapshot`). Do **not** hunt these tiny icons with `web_page_coords`/vision: vision finds large text (titles) reliably but whiffs on small unlabeled arrows. Fall back to vision only if an arrow genuinely has no ref. The arrow changes color when active — confirm it took; don't double-click.
- **Comment**: click into the comment box under the post or comment, type with `web_type_focused`, then click **save**. Read the thread first so the comment fits and isn't a repeat of what's there.
- **Submit a post**: go to `https://www.reddit.com/r/<sub>/submit`, pick self/link, fill the title + body with `web_type_focused`, follow the sub's flair/format rules, then submit.
- After each action let the page settle and **confirm it took** (arrow lit, comment appeared) before the next one. Don't queue blind clicks.

## Don't get the account flagged
- **Never burst.** Space actions out — a human doesn't upvote 30 things in 10 seconds. Pace per `web-interaction`.
- **Never vote or post from this account AND the user's own on the same content** — same device and IP, which Reddit reads as manipulation (see `emi-reddit-safety`).
- If Reddit shows a CAPTCHA, a "you're doing that too much" cooldown, or a verify-you're-human wall: **stop and hand off** (`return_control`). Do not retry through it — that's what gets the account banned.

## Only as herself
This is her account on her session. Engage Reddit **only when `acting_as: self`.** If acting as the user, do not drive Reddit as her.
