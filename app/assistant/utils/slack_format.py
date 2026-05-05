"""GitHub-flavored Markdown → Slack mrkdwn conversion.

LLMs write standard Markdown by default — ``**bold**``, ``[text](url)``,
``# headings``. Slack's ``mrkdwn`` dialect is different: bold is single
asterisks, links are ``<url|text>``, no native headings. Without
conversion, ``**P!NK**`` renders literally as ``**P!NK**`` in the Slack
client (asterisks visible) instead of bold.

Run ``to_slack_mrkdwn`` on every outbound Slack message body, just before
the API call. Idempotent on already-converted text in practice — the
conversion only matches GFM-specific patterns that aren't valid mrkdwn.

Conversions applied:
  ``**bold**`` / ``__bold__``        → ``*bold*``
  ``[text](https://url)``            → ``<https://url|text>``
  ``# Heading`` / ``## Heading``     → ``*Heading*``  (Slack has no native heading)

Deliberately NOT converted (would risk breaking already-correct text):
  Single-asterisk ``*italic*``       — overlaps with Slack bold, ambiguous
  Underscore ``_italic_``            — already valid Slack syntax
  Backticks ``code``                 — already valid Slack syntax
  Triple-backtick code fences        — already valid Slack syntax
"""
from __future__ import annotations

import re

# **bold** → *bold* (non-greedy, no nested **)
_GFM_BOLD_STAR = re.compile(r"\*\*([^\s*][^*]*?[^\s*]|\S)\*\*")

# __bold__ → *bold* (parallel rule for the underscore form of GFM bold)
_GFM_BOLD_UNDERSCORE = re.compile(r"__([^\s_][^_]*?[^\s_]|\S)__")

# [text](https://url) → <https://url|text>
# Captures: 1=text, 2=url. Bare URLs (no anchor text) handled by Slack natively.
_GFM_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")

# Leading '# ' / '## ' / etc on its own line → bold the line.
_GFM_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)\s*$", re.MULTILINE)


def to_slack_mrkdwn(text: str) -> str:
    """Convert GFM-style markdown in ``text`` to Slack mrkdwn."""
    if not text:
        return text or ""
    out = text
    # Order matters: do **bold** / __bold__ BEFORE link conversion,
    # so we don't accidentally consume asterisks inside link text.
    out = _GFM_BOLD_STAR.sub(r"*\1*", out)
    out = _GFM_BOLD_UNDERSCORE.sub(r"*\1*", out)
    out = _GFM_LINK.sub(r"<\2|\1>", out)
    out = _GFM_HEADING.sub(r"*\2*", out)
    return out
