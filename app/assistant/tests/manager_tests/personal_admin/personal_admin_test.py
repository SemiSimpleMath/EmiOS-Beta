"""personal_admin_manager smoke test.

Runs through the generic _runner so the bootstrap is one-liner thin.
Edit task / info below or pass via CLI.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Repo on sys.path for direct invocation.
_REPO = Path(__file__).resolve().parents[5]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.assistant.tests.manager_tests._runner import run_manager_test


# Default smoke task — read-only, exercises get_calendar_events + the
# JSON-args fast path. Read-only so repeat runs don't spam mailboxes /
# create test artifacts. send_email requires the approval gateway, which
# needs an active room session not available in this test context.
DEFAULT_TASK = "What calendar events does Jukka have today?"
DEFAULT_INFO = ""


def main():
    run_manager_test(
        manager_type="personal_admin_manager",
        task=DEFAULT_TASK,
        info=DEFAULT_INFO,
    )


if __name__ == "__main__":
    main()
