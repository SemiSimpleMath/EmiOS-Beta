from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    value = str(os.getenv(name, default) or "").strip()
    if not value:
        raise ValueError(f"{name} resolved to empty value.")
    return value


DEFAULT_GOOGLE_ACCOUNT_ID = _env("EMI_GOOGLE_DEFAULT_ACCOUNT_ID", "google_user_primary")
GMAIL_GOOGLE_ACCOUNT_ID = _env("EMI_GOOGLE_GMAIL_ACCOUNT_ID", DEFAULT_GOOGLE_ACCOUNT_ID)
CALENDAR_GOOGLE_ACCOUNT_ID = _env("EMI_GOOGLE_CALENDAR_ACCOUNT_ID", DEFAULT_GOOGLE_ACCOUNT_ID)
TASKS_GOOGLE_ACCOUNT_ID = _env("EMI_GOOGLE_TASKS_ACCOUNT_ID", DEFAULT_GOOGLE_ACCOUNT_ID)
NEST_GOOGLE_ACCOUNT_ID = _env("EMI_GOOGLE_NEST_ACCOUNT_ID", "google_nest")

