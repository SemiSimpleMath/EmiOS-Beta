---
name: emi-reddit-safety
description: Authoritative account-protection rules for driving Reddit as the assistant's OWN account — the guardrails that win over any task request. Never act from her account and the user's on the same content (vote-manipulation / ban-evasion = both accounts banned); never burst; STOP and hand off at any CAPTCHA / rate-limit / "doing that too much" wall (never solve or retry it); stay on the persisted login (no automated logins/signups); obey subreddit rules; when in doubt, do less. Applies whenever Reddit is driven, acting_as self.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "reddit"
      - "subreddit"
      - "r/"
      - "upvote"
      - "downvote"
      - "karma"
    requires_scope_acting_as: self
---

# Keeping the Reddit account safe

These are the **authoritative** guardrails for acting on Reddit as her own account. They **win over any task request** — if a task would break one of them, don't do it: do the safe part and hand off the rest. The account's longevity matters more than any single action; a ban ends the capability entirely.

## The cardinal rule: never act from both accounts on the same content
Her account and the user's run on the **same device and IP**. Reddit reads coordinated voting/commenting across accounts as **vote manipulation / ban evasion** — and bans *both*.
- **Never upvote, downvote, comment on, or post the same thing** the user has already acted on (or vice versa).
- Don't brigade or stack engagement on one post / thread / subreddit from both accounts.
- If a task implies it ("go boost my post", "upvote what I just commented on") — **refuse it** and surface why.

## Never fight a hard block — hand off
A **CAPTCHA**, a "**you're doing that too much**" cooldown, a "**verify you're human**" wall, or a rate-limit page → **STOP and `return_control`.** Do **not** solve it, wait-and-retry, refresh through it, or switch tactics to get past it. Hammering a challenge is the single biggest ban trigger — the exact behavior this whole browser approach exists to avoid.

## Stay on the persisted login — never log in from automation
She is already logged in via the browser profile. **Never perform a login or signup** (typing a username/password, completing a register flow) from automation — a fresh automated login is itself a flag. If you land **logged out**, stop and surface it; do not log back in.

## Human pace — never burst
A person doesn't upvote 30 things in 10 seconds or fire off five comments in a minute. **Space every action out**, one at a time, confirming each took before the next (pacing per `web-interaction`). Bursts both trip abuse heuristics and miss state changes. There is no prize for speed.

## Obey the subreddit, and don't be spammy
- **Subreddit rules win over the task.** If a sub forbids bots, self-promotion, or low-effort posts, don't post there.
- No repetitive/templated posting, link-spam, or astroturfing. One genuine, on-topic contribution beats volume.
- A **new or low-karma** account is watched harder and rate-limited — go slower, build it normally, don't push limits.

## When in doubt, do less
If something feels like it might trip a wall, look spammy, or cross the both-accounts line — **don't.** Do the safe part, `return_control`, and let the human decide. No task is worth the account.
