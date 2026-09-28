"""Secrets stay out of the belief store.

Evidence is kept verbatim, and people paste credentials into chat ("the account is X and the
password is Y"). The 185-day replay stored one such password. Every text the intake keeps —
evidence, the prompt it answered, a statement — passes through here first. The account, the
address and the fact that a credential was shared all stay; the credential itself becomes
<redacted>.
"""
from __future__ import annotations

import re

REDACTED = "<redacted>"

_RULES = [
    # "password is hunter2", "passcode: 1234", "pin = 0000" — the word, an explicit link, the value
    (re.compile(r"(?i)\b(pass(?:word|code|phrase)|pwd|pin)\b(\s*(?:is|:|=)\s*)(\S+)"), rf"\1\2{REDACTED}"),
    # "api key is ...", "token: ...", "secret = ..." — same shape, a value of credential length
    (re.compile(r"(?i)\b(api[ _-]?key|access key|secret|token|bearer)\b(\s*(?:is|:|=)\s*)([A-Za-z0-9_\-./+]{8,})"),
     rf"\1\2{REDACTED}"),
    # Keys recognisable by prefix, wherever they appear
    (re.compile(r"\b(?:[sr]k|pk)_(?:live|test)_[A-Za-z0-9]{8,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bgh[pous]_[A-Za-z0-9]{20,}\b"), REDACTED),
    (re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), REDACTED),
]


def redact(text: str | None) -> str | None:
    if not text:
        return text
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
