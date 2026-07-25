---
name: web-interaction
description: How to click and interact with a live web page through the browser tools — look before acting, clear cookie/consent/login overlays first, act by accessibility ref (vision only as fallback), move at a human pace, keep credentials confined, and hand off (never fight) CAPTCHAs and hard bot-blocks. General behavior for any site driven via Playwright; site-specific skills and form-filling layer on top.
license: Apache-2.0
metadata:
  author: emi-team
  version: "1.0"
  auto_inject_when:
    task_keywords:
      - "click"
      - "browse"
      - "navigate to"
      - "on the website"
      - "web page"
---

# Interacting with a web page

Default behavior for driving any live site with the browser tools. Site-specific skills (e.g. `doordash-ordering`) and `form-filling` extend this; the planner's own rules already cover click-confidence and the scroll → ref → vision recovery ladder — this skill is the behavior *around* them.

## Know where the browser is
- A fresh browser session opens on a **blank tab** (`about:blank`) — on a new task, your first action is `web_navigate_snapshot` to the target site's URL. The "Current page" line in your status and each tool result's `url` tell you where the browser is; page tools find elements once the site you expect is loaded.

## Look before you act
- Get the page's actionable structure first: `web_modal_scan` (accessibility refs — buttons, links, inputs) is the cheap, deterministic default. Reach for `web_page_coords` (vision: screenshot → reasoned coordinates) only when the snapshot is missing or ambiguous (custom/canvas controls, unlabeled `[element]`s).
- Act by **ref**, never by guessed coordinates. Typing round-number x/y and hoping is how clicks miss and find → guess → re-find loops start.

## Clear the path first
Most pages put something in front of the content; dismiss it before pursuing the goal:
- **Cookie / consent banners** — accept or close so they stop intercepting clicks.
- **Newsletter / promo modals, age gates, "open in app" interstitials** — close them.
- A click that "does nothing" is usually an invisible overlay eating it. Scan for the overlay — don't just re-click the target.

## Move like a person, not a script
- One action, then read the *settled* snapshot before the next — don't queue blind clicks.
- Act at a deliberate, even pace. Bursts both miss state changes and trip a site's abuse heuristics. There is no prize for speed.

## Keep credentials confined
- If content needs a session that isn't present, stop and surface it — don't guess or brute a login.
- For any secret field (password, one-time code, account number) use `web_type_secret` (pod → field, at courier scope). Never `pod_fetch` then type, and never type a credential you can read.

## Don't fight a hard block
A CAPTCHA, a "verify you're human" challenge, or a bot-block page → **do not try to solve or bypass it.** Stop and hand control to the user (`return_control` / a review ticket). Hammering a challenge is exactly what flags an account — the thing we are avoiding.

## Stop before the irreversible
Before a final, hard-to-undo commit — placing an order, confirming a payment, sending, deleting — stop and `return_control` (or raise a review ticket). Staging the action is fine; committing the side effect waits for the human.
