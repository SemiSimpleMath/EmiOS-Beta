---
name: slack-formatting
description: Slack uses its own mrkdwn dialect, not GitHub-flavored Markdown. Use this when responding on Slack — when room_surface == "slack". Defines the substitutions to apply (** → *, *italic* → _italic_, [text](url) → <url|text>, # heading → bold line) and lists the gotchas. Do NOT apply on UI / SMS / Telegram / Email.
license: Apache-2.0
metadata:
  author: jukka
  version: "1.0"
---

# Slack formatting

When responding on Slack (`room_surface == "slack"`), use Slack's *mrkdwn*
dialect, not standard Markdown. Standard Markdown like `**bold**` renders
literally with the asterisks visible.

Apply these substitutions on Slack only:

| GFM         | Slack mrkdwn   |
|-------------|----------------|
| `**bold**`  | `*bold*`       |
| `*italic*`  | `_italic_`     |
| `# heading` | (no native — bold the line: `*Heading*`) |
| `[text](url)` | `<url\|text>` |

Notes:
- Single-asterisk (`*bold*`) is **bold** in Slack — do NOT use it for italic.
- Backticks for inline code (`` `code` ``) and triple-backtick code fences
  work the same way as GFM. Keep those unchanged.
- Underscore italic (`_italic_`) and tildes for strikethrough (`~strike~`)
  are native to mrkdwn.
- Bullet lists (`- item`) and numbered lists work natively.
- Block quotes use `>` at the start of the line.

On other surfaces (UI, SMS, Telegram, Email), use standard Markdown — the
Slack-specific rules above do NOT apply.
