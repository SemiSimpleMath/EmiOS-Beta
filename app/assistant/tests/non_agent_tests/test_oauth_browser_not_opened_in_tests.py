"""A test run must never open the user's OAuth consent tab.

Any test that reaches a Google client without stored credentials calls
``_open_oauth_browser``, which opens a real browser tab on the developer's
desktop. It happened twice — once while the app was not running at all, which reads
as the app spontaneously demanding re-authentication.

``EMI_TEST_MODE`` is set by ``app/assistant/tests/test_setup.py``, which every
test bootstraps through, so the guard lives in the credential loader itself and
covers pytest and the standalone manager harnesses alike.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_oauth_browser_not_opened_in_tests.py
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

import os

import pytest

from app.assistant.lib.google_auth import google_credentials


@pytest.fixture(autouse=True)
def _clear_debounce():
    """The opener debounces per account; start each test with a clean slate."""
    google_credentials._oauth_browser_last_opened.clear()
    yield
    google_credentials._oauth_browser_last_opened.clear()


def test_no_browser_opens_while_emi_test_mode_is_set(monkeypatch):
    """The condition every test in this repo actually runs under."""
    assert os.getenv("EMI_TEST_MODE") == "1", "test_setup should have set this"

    opened = []
    monkeypatch.setattr(google_credentials.webbrowser, "open", lambda *a, **k: opened.append(a))

    google_credentials._open_oauth_browser("google_user_primary")

    assert opened == [], "a test run opened the user's browser"


def test_browser_opens_when_not_a_test_process(monkeypatch):
    """The guard must not disable the feature for the real app."""
    monkeypatch.setenv("EMI_TEST_MODE", "")
    monkeypatch.setenv("EMI_AUTO_OPEN_GOOGLE_OAUTH", "1")

    opened = []
    monkeypatch.setattr(google_credentials.webbrowser, "open", lambda *a, **k: opened.append(a))

    google_credentials._open_oauth_browser("google_user_primary")

    assert len(opened) == 1, "production should still prompt for re-auth"
    assert "/oauth/google/start" in opened[0][0]


def test_explicit_opt_out_still_honoured(monkeypatch):
    """EMI_AUTO_OPEN_GOOGLE_OAUTH=0 keeps working independently of test mode."""
    monkeypatch.setenv("EMI_TEST_MODE", "")
    monkeypatch.setenv("EMI_AUTO_OPEN_GOOGLE_OAUTH", "0")

    opened = []
    monkeypatch.setattr(google_credentials.webbrowser, "open", lambda *a, **k: opened.append(a))

    google_credentials._open_oauth_browser("google_user_primary")

    assert opened == []
