"""Reliability spine R2: transient classification + bounded retry + one re-ask on bad output.

Covers the SSOT classifier (is_transient), the retry helper (transient retries then fails loud;
non-transient and success do not retry), and the re-ask gate on LLMClient (valid output passes
through with no re-ask; an invalid Pydantic-model output triggers exactly one corrective re-ask;
non-model formats and already-validated model instances pass through untouched).
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.services.llm_resilience import is_transient, retry_transient
from app.assistant.agent_runtime.services.llm_client import LLMClient


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


# ── re-ask gate ───────────────────────────────────────────────────
class _Form(BaseModel):
    x: int


class _Agent:
    name = "tester"


def _client():
    return LLMClient.__new__(LLMClient)   # _revalidate_or_reask uses no instance state


class TestReask:
    def test_valid_dict_passes_through_no_reask(self):
        calls = []
        out = _client()._revalidate_or_reask(
            agent=_Agent(), response={"x": 5}, response_format=_Form,
            invoke=lambda m: calls.append(m), messages=[{"role": "user", "content": "hi"}], engine="e",
        )
        assert out == {"x": 5}
        assert calls == []   # valid → no re-ask

    def test_invalid_dict_reasks_once(self):
        original = [{"role": "user", "content": "hi"}]
        seen = []
        def invoke(msgs):
            seen.append(msgs)
            return {"x": 7}
        out = _client()._revalidate_or_reask(
            agent=_Agent(), response={"nope": 1}, response_format=_Form,
            invoke=invoke, messages=original, engine="e",
        )
        assert out == {"x": 7}           # returned the re-ask result
        assert len(seen) == 1            # exactly one re-ask
        appended = seen[0][-1]
        assert appended["role"] == "user" and "schema" in appended["content"].lower()
        assert original == [{"role": "user", "content": "hi"}]   # original not mutated

    def test_non_model_format_passes_through(self):
        calls = []
        out = _client()._revalidate_or_reask(
            agent=_Agent(), response={"anything": 1}, response_format=None,
            invoke=lambda m: calls.append(m), messages=[], engine="e",
        )
        assert out == {"anything": 1} and calls == []

    def test_model_instance_passes_through(self):
        calls = []
        inst = _Form(x=3)
        out = _client()._revalidate_or_reask(
            agent=_Agent(), response=inst, response_format=_Form,
            invoke=lambda m: calls.append(m), messages=[], engine="e",
        )
        assert out is inst and calls == []
