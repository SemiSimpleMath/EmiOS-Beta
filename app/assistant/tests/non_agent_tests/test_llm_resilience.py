"""Reliability spine R2: transient classification + bounded retry.

Covers the SSOT classifier (is_transient) and the retry helper (transient retries then fails loud;
non-transient and success do not retry). The re-ask on a refused answer moved to
agent_runtime/correction.py on 2026-11-15 (tests: agent_tests/test_agent_output_correction.py).
"""
from __future__ import annotations

import pytest

from app.services.llm_resilience import is_transient, retry_transient


# ── classifier ────────────────────────────────────────────────────
class TestIsTransient:
    @pytest.mark.parametrize("msg", [
        "Error: 503 Service Unavailable",
        "429 Too Many Requests",
        "anthropic.InternalServerError: overloaded_error (529)",
        "sqlite3.OperationalError: database is locked",
        "Connection reset by peer",
        "[WinError 10054] An existing connection was forcibly closed by the remote host",
        "APIConnectionError: Connection error.",
    ])
    def test_transient(self, msg):
        assert is_transient(Exception(msg)) is True

    @pytest.mark.parametrize("msg", [
        "insufficient_quota: you have exceeded your current quota",
        "LLM request timed out after 240 seconds",   # timeouts are NOT retried here
        "pydantic ValidationError: 2 validation errors",
        "invalid api key",
        "ValueError: model must be a non-empty string",
    ])
    def test_not_transient(self, msg):
        assert is_transient(Exception(msg)) is False

    def test_real_connection_error_by_type(self):
        # A real ConnectionResetError (subclass of ConnectionError) is caught by type even with no
        # telling message.
        assert is_transient(ConnectionResetError()) is True

    def test_quota_beats_rate_limit(self):
        # "429 + quota" is billing (fatal), not a plain rate-limit — must NOT be retried.
        assert is_transient(Exception("429: insufficient_quota")) is False


# ── retry helper ──────────────────────────────────────────────────
class TestRetryTransient:
    def test_success_first_try_no_retry(self):
        calls = {"n": 0}
        def _f():
            calls["n"] += 1
            return "ok"
        assert retry_transient(_f, attempts=3, base_delay=0.0) == "ok"
        assert calls["n"] == 1

    def test_transient_retries_then_fails_loud(self):
        calls = {"n": 0}
        def _f():
            calls["n"] += 1
            raise RuntimeError("503 service unavailable")
        with pytest.raises(RuntimeError):
            retry_transient(_f, attempts=3, base_delay=0.0)
        assert calls["n"] == 3   # all attempts used, then re-raised

    def test_non_transient_raises_immediately(self):
        calls = {"n": 0}
        def _f():
            calls["n"] += 1
            raise ValueError("bad input")
        with pytest.raises(ValueError):
            retry_transient(_f, attempts=3, base_delay=0.0)
        assert calls["n"] == 1   # no retry

    def test_recovers_on_second_attempt(self):
        calls = {"n": 0}
        def _f():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("temporarily unavailable")
            return "recovered"
        assert retry_transient(_f, attempts=3, base_delay=0.0) == "recovered"
        assert calls["n"] == 2
