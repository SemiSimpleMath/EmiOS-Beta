from __future__ import annotations

SURFACE_SLACK = "slack"
SURFACE_TELEGRAM = "telegram"
SURFACE_SMS = "sms"
SURFACE_UI = "ui"

# Surfaces that support inline text-based approval (via QuestionService).
INLINE_APPROVAL_SURFACES: frozenset[str] = frozenset({SURFACE_TELEGRAM, SURFACE_SMS, SURFACE_SLACK})

# Default authority levels by surface when not explicitly set in the room's policy.json.
# New surfaces default to 0 unless added here.
DEFAULT_AUTHORITY_BY_SURFACE: dict[str, int] = {
    SURFACE_SLACK: 50,
    SURFACE_TELEGRAM: 40,
    SURFACE_SMS: 40,
}
