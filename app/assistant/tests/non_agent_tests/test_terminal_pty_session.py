"""Terminal session guards — environment scrubbing and geometry clamping.

The pty mechanics themselves need a real `claude` process and a real ConPTY, so
they are exercised by running the console, not here. What IS worth locking down
is the stuff that is silent when it breaks:

- If EmiOS's ``ANTHROPIC_API_KEY`` leaks into the child, the embedded terminal
  bills the API instead of using the user's subscription, and nothing says so.
- If a ``CLAUDE_CODE_*`` child-session marker leaks in, the CLI stops saving
  transcripts and only mentions it in a startup line nobody reads.
- If a client's geometry reaches the pty unclamped, a bad resize can wedge it.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_terminal_pty_session.py
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

import pytest

from app.assistant.terminal import pty_session
from app.assistant.terminal import socket_handlers


# ---------------------------------------------------------------- environment


def test_child_env_strips_anthropic_credentials(monkeypatch):
    """An inherited API key would silently divert billing off the subscription."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-reach-the-cli")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-should-not-reach-the-cli")

    env = pty_session._child_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env


def test_child_env_strips_claude_code_markers(monkeypatch):
    """A nested-session marker makes the CLI stop saving transcripts."""
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")

    env = pty_session._child_env()

    assert not [k for k in env if k.startswith("CLAUDE_CODE_")]


def test_child_env_keeps_the_rest_of_the_environment(monkeypatch):
    """Only the named markers go; the child still needs PATH and friends."""
    monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")

    env = pty_session._child_env()

    assert env["SOME_UNRELATED_VAR"] == "keep-me"
    assert "PATH" in env or "Path" in env


def test_child_env_declares_a_color_terminal():
    """Claude Code paints a full-screen UI; it needs to know the pty is xterm."""
    assert pty_session._child_env()["TERM"] == "xterm-256color"


# ------------------------------------------------------------------- geometry


@pytest.mark.parametrize(
    "given, expected",
    [
        ({"rows": 40, "cols": 120}, (40, 120)),          # ordinary
        ({"rows": 0, "cols": 0}, (30, 100)),             # falsy -> default
        ({"rows": 1, "cols": 2}, (4, 20)),               # below the floor
        ({"rows": 9999, "cols": 9999}, (300, 600)),      # above the ceiling
        ({"rows": "nope", "cols": "nope"}, (30, 100)),   # unparseable
        (None, (30, 100)),                               # no payload at all
    ],
)
def test_geometry_is_clamped(given, expected):
    assert socket_handlers._geometry(given) == expected


# ----------------------------------------------------------------- terminal id


def test_terminal_id_defaults_when_absent():
    assert socket_handlers._terminal_id(None) == socket_handlers.DEFAULT_TERMINAL_ID
    assert socket_handlers._terminal_id({}) == socket_handlers.DEFAULT_TERMINAL_ID
    assert socket_handlers._terminal_id({"terminal_id": "  "}) == socket_handlers.DEFAULT_TERMINAL_ID


def test_terminal_id_is_taken_from_the_payload():
    assert socket_handlers._terminal_id({"terminal_id": " other "}) == "other"


# -------------------------------------------------------------------- loopback


def test_loopback_set_covers_the_addresses_a_local_browser_presents():
    """Both IPv4 and IPv6 localhost, including the v4-mapped form."""
    assert "127.0.0.1" in socket_handlers._LOOPBACK_ADDRS
    assert "::1" in socket_handlers._LOOPBACK_ADDRS
    assert "::ffff:127.0.0.1" in socket_handlers._LOOPBACK_ADDRS


def test_loopback_set_excludes_routable_addresses():
    """A LAN or tunnel peer must not match — this is the only access gate."""
    for addr in ("192.168.1.50", "10.0.0.7", "0.0.0.0", "203.0.113.9", ""):
        assert addr not in socket_handlers._LOOPBACK_ADDRS
