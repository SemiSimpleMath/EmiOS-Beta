"""personal_admin_manager smoke test.

Runs through the generic _runner so the bootstrap is one-liner thin.
Edit task / info below or pass via CLI.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Repo on sys.path for direct invocation.
_REPO = Path(__file__).resolve().parents[4]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.assistant.tests.manager_tests._runner import run_manager_test


# Default smoke task — exercises send_email + the JSON-args fast path.
DEFAULT_TASK = "Send a test email to Jukka."
DEFAULT_INFO = (
    "Recipient: Jukka Virtanen (semisimplemath@gmail.com). "
    "Subject: Test Email. "
    "Body: This is a test email from Emi to help with your tinkering."
)


def main():
    run_manager_test(
        manager_type="personal_admin_manager",
        task=DEFAULT_TASK,
        info=DEFAULT_INFO,
    )


if __name__ == "__main__":
    main()
