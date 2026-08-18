"""Quota taxonomy (LLM audit L1, 2026-07-09).

Two contradictory quota mechanisms coexisted: the provider layer raised a
catchable error while the interface layer os._exit(1)'d on ANY exception
matching ("429" and "quota") — a pattern Gemini per-minute rate windows
also match, so a transient RPM burst could kill the whole assistant.

Now the provider boundary translates the SDKs' STRUCTURED markers (OpenAI
error.code, Gemini QuotaFailure.quotaId + RetryInfo.retryDelay) into
BillingQuotaExhausted (interface stops the world — the hard stop is
deliberate: batch loops must never march on marking rows attempted with
no results) or TransientRateLimit (retry ladder). String matching remains
only for unstructured shapes and is conservative: ambiguous quota → stop.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import llm_client as lc
from app.services.llm_resilience import (
    BillingQuotaExhausted,
    TransientRateLimit,
    classify_quota,
    is_transient,
    retry_transient,
)


# ── classify_quota: the ONE judgement ─────────────────────────────


class TestClassifyQuota:
    def test_canonical_types_win(self):
        assert classify_quota(BillingQuotaExhausted(provider="openai")) == "billing"
        assert classify_quota(TransientRateLimit()) == "rate"

    def test_gemini_per_minute_wording_is_rate(self):
        exc = Exception(
            "429 RESOURCE_EXHAUSTED. You exceeded your current quota. "
            "quota_metric: generate_content_requests_per_minute"
        )
        assert classify_quota(exc) == "rate"

    def test_billing_wording_is_billing(self):
        exc = Exception("You exceeded your current quota, please check your plan and billing details.")
        assert classify_quota(exc) == "billing"

    def test_ambiguous_quota_is_billing_conservative(self):
        assert classify_quota(Exception("project quota reached")) == "billing"

    def test_no_credits_wording_is_billing(self):
        # OpenAI's credits billing model says "no credits remaining" —
        # the word "quota" never appears in the message. Unstructured
        # arrivals of that wording must still stop the world.
        exc = Exception(
            "Error code: 429 - You have no credits remaining. "
            "Add credits to continue using the API."
        )
        assert classify_quota(exc) == "billing"
        assert is_transient(exc) is False

    def test_non_quota_is_none(self):
        assert classify_quota(Exception("503 Service Unavailable")) is None


class TestIsTransientQuota:
    def test_rate_window_retries(self):
        assert is_transient(TransientRateLimit("429 per minute")) is True

    def test_billing_never_retries(self):
        assert is_transient(BillingQuotaExhausted(provider="gemini")) is False

    def test_unstructured_per_minute_quota_retries(self):
        assert is_transient(Exception("429: quota exceeded (requests per minute)")) is True


# ── provider translation: structured markers ──────────────────────


class _FakeOpenAIError(Exception):
    def __init__(self, message: str, code=None, type_=None):
        super().__init__(message)
        self.code = code
        self.type = type_


class _FakeRateLimitError(lc.OpenAIRateLimitError):
    # Bypass the SDK constructor (it demands an httpx.Response).
    def __init__(self, message="429 Too Many Requests", retry_after=None):
        Exception.__init__(self, message)
        self.code = "rate_limit_exceeded"
        headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
        self.response = SimpleNamespace(headers=headers)


class TestOpenAITranslation:
    def test_insufficient_quota_code_is_billing(self):
        out = lc._translate_openai_exception(
            _FakeOpenAIError("Error 429", code="insufficient_quota")
        )
        assert isinstance(out, BillingQuotaExhausted)
        assert out.provider == "openai"

    def test_rate_limit_error_is_transient_with_retry_after(self):
        out = lc._translate_openai_exception(_FakeRateLimitError(retry_after=7))
        assert isinstance(out, TransientRateLimit)
        assert out.retry_after == 7.0

    def test_billing_beats_rate_limit_type(self):
        # OpenAI billing exhaustion IS a RateLimitError — code decides.
        exc = _FakeRateLimitError()
        exc.code = "insufficient_quota"
        assert isinstance(lc._translate_openai_exception(exc), BillingQuotaExhausted)

    def test_credit_balance_exhausted_is_billing(self):
        # The 2026-08-17 incident: the credits billing model returns
        # code="credit_balance_exhausted" with type="insufficient_quota".
        # A truthy unknown code must not shadow the type marker.
        exc = _FakeRateLimitError(
            "Error code: 429 - {'error': {'message': 'You have no credits "
            "remaining. Add credits to continue using the API at "
            "https://platform.openai.com/settings/organization/billing/.', "
            "'type': 'insufficient_quota', 'param': None, "
            "'code': 'credit_balance_exhausted'}}"
        )
        exc.code = "credit_balance_exhausted"
        exc.type = "insufficient_quota"
        out = lc._translate_openai_exception(exc)
        assert isinstance(out, BillingQuotaExhausted)
        assert out.provider == "openai"

    def test_insufficient_quota_type_alone_is_billing(self):
        # Same guard without RateLimitError involved: type carries the
        # marker while code holds unrelated truthy junk.
        exc = _FakeOpenAIError(
            "Error 429", code="some_new_code", type_="insufficient_quota"
        )
        assert isinstance(lc._translate_openai_exception(exc), BillingQuotaExhausted)

    def test_non_quota_returns_none(self):
        assert lc._translate_openai_exception(_FakeOpenAIError("500 boom")) is None


# ── OpenCode Zen: plan-level caps arrive as an ordinary 429 ───────
#
# OpenAI-compatible third parties do not set error.code == "insufficient_quota".
# OpenCode returns a 429 whose body carries GoUsageLimitError plus the window
# name, so without an explicit check it reads as a per-minute rate limit and
# gets retried — for as long as the cap lasts (observed: 3 days).

_OPENCODE_WEEKLY_429 = (
    "Error code: 429 - {'type': 'error', 'error': {'type': 'GoUsageLimitError', "
    "'message': 'Weekly usage limit reached. Resets in 3 days. To continue using "
    "this model now, enable usage from your available balance: "
    "https://opencode.ai/workspace/wrk_TEST/go'}, "
    "'metadata': {'workspace': 'wrk_TEST', 'limitName': 'weekly'}}"
)


class TestOpenCodeUsageLimits:
    def test_weekly_usage_limit_is_billing_not_transient(self):
        out = lc._translate_openai_exception(
            _FakeRateLimitError(_OPENCODE_WEEKLY_429), provider="opencode"
        )
        assert isinstance(out, BillingQuotaExhausted)
        assert out.provider == "opencode"
        assert is_transient(out) is False

    def test_weekly_limit_without_sdk_type_still_billing(self):
        out = lc._translate_openai_exception(
            _FakeOpenAIError(_OPENCODE_WEEKLY_429), provider="opencode"
        )
        assert isinstance(out, BillingQuotaExhausted)

    def test_per_minute_rate_limit_still_retries(self):
        # A genuine short-window 429 must stay retryable.
        out = lc._translate_openai_exception(
            _FakeRateLimitError("429 rate_limit_exceeded: requests per minute"),
            provider="opencode",
        )
        assert isinstance(out, TransientRateLimit)
        assert is_transient(out) is True

    def test_openai_translation_unchanged_by_default_provider(self):
        out = lc._translate_openai_exception(
            _FakeOpenAIError("Error 429", code="insufficient_quota")
        )
        assert out.provider == "openai"

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("Weekly usage limit reached", True),
            ("GoUsageLimitError", True),
            ("{'limitName': 'monthly'}", True),
            ("429 requests per minute", False),
            ("500 internal error", False),
        ],
    )
    def test_marker_detection(self, message, expected):
        assert lc._is_long_window_usage_limit(Exception(message)) is expected


class _FakeGeminiError(Exception):
    def __init__(self, message, code=429, status="RESOURCE_EXHAUSTED", details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _gemini_details(quota_id: str, retry_delay: str = "37s") -> dict:
    return {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [{"quotaId": quota_id}],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay},
            ],
        }
    }


class TestGeminiTranslation:
    def test_per_minute_quota_id_is_transient_with_retry_delay(self):
        exc = _FakeGeminiError(
            "429 RESOURCE_EXHAUSTED. You exceeded your current quota.",
            details=_gemini_details("GenerateRequestsPerMinutePerProjectPerModel"),
        )
        out = lc._translate_gemini_exception(exc)
        assert isinstance(out, TransientRateLimit)
        assert out.retry_after == 37.0

    def test_per_day_quota_id_is_billing(self):
        exc = _FakeGeminiError(
            "429 RESOURCE_EXHAUSTED. You exceeded your current quota.",
            details=_gemini_details("GenerateRequestsPerDayPerProjectPerModel"),
        )
        out = lc._translate_gemini_exception(exc)
        assert isinstance(out, BillingQuotaExhausted)
        assert out.provider == "gemini"

    def test_429_without_details_but_per_minute_message_is_transient(self):
        exc = _FakeGeminiError(
            "429 RESOURCE_EXHAUSTED: quota_metric requests per minute", details=None,
        )
        assert isinstance(lc._translate_gemini_exception(exc), TransientRateLimit)

    def test_429_without_details_or_wording_stops_the_world(self):
        exc = _FakeGeminiError("429 RESOURCE_EXHAUSTED: quota", details=None)
        assert isinstance(lc._translate_gemini_exception(exc), BillingQuotaExhausted)

    def test_non_429_returns_none(self):
        exc = _FakeGeminiError("504 Deadline Exceeded", code=504, status="DEADLINE_EXCEEDED")
        assert lc._translate_gemini_exception(exc) is None


# ── the kill switch fires only on billing ─────────────────────────


@pytest.fixture
def _breaker_reset():
    lc._quota_tripped.clear()
    yield
    lc._quota_tripped.clear()


class TestKillSwitch:
    def test_billing_trips_and_exits(self, monkeypatch, _breaker_reset):
        exits = []
        monkeypatch.setattr(lc.os, "_exit", lambda code: exits.append(code))
        import time as _time_mod
        monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
        lc._check_and_trip_quota("openai", BillingQuotaExhausted(provider="openai"))
        assert exits == [1]
        assert lc._quota_tripped.get("openai") is True

    def test_rate_window_never_reaches_the_kill(self, monkeypatch, _breaker_reset):
        exits = []
        monkeypatch.setattr(lc.os, "_exit", lambda code: exits.append(code))
        out = lc._check_and_trip_quota(
            "gemini", TransientRateLimit("429 per minute", retry_after=12),
        )
        assert out is False
        assert exits == []
        assert not lc._quota_tripped

    def test_unstructured_billing_string_still_kills(self, monkeypatch, _breaker_reset):
        exits = []
        monkeypatch.setattr(lc.os, "_exit", lambda code: exits.append(code))
        import time as _time_mod
        monkeypatch.setattr(_time_mod, "sleep", lambda s: None)
        lc._check_and_trip_quota(
            "openai",
            Exception("insufficient_quota: you exceeded your current quota"),
        )
        assert exits == [1]


# ── talking about quotas is not a quota error (audit L2) ─────────


def test_agent_output_discussing_quota_passes_through(monkeypatch):
    """The old success-path scanner raised QuotaExhaustedError when a
    perfectly good response CONTAINED phrases like "quota exceeded" —
    an agent explaining an API error would crash its own turn. Real quota
    failures arrive as typed exceptions, never as response content."""
    from types import SimpleNamespace

    from app.assistant.agent_runtime.services.llm_client import LLMClient

    client = LLMClient()
    response = {
        "final_answer_content": "OpenAI returns 'quota exceeded' / 'insufficient quota' "
        "when billing lapses; 'rate limit exceeded' means slow down.",
    }
    fake_interface = SimpleNamespace(
        structured_output=lambda msgs, use_json=False, **p: response,
    )
    monkeypatch.setattr(
        LLMClient, "get_llm_interface", lambda self, *, agent: fake_interface,
    )

    class _BB:
        def get_state_value(self, key):
            return None

    agent = SimpleNamespace(
        name="tester",
        llm_params={"llm_provider": "openai", "engine": "gpt-5-mini"},
        blackboard=_BB(),
        config={},
    )
    out = client.call_structured_output(
        agent=agent,
        messages=[{"role": "user", "content": "what do these API errors mean?"}],
        response_format=None,
    )
    assert out == response


# ── retry ladder honors the provider-suggested wait ───────────────


def test_retry_transient_uses_retry_after(monkeypatch):
    import app.services.llm_resilience as lr

    sleeps = []
    monkeypatch.setattr(lr.time, "sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def _f():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientRateLimit("429 per minute", retry_after=9.0)
        return "ok"

    assert retry_transient(_f, attempts=3, base_delay=0.5) == "ok"
    assert sleeps == [9.0]  # provider hint beats the 0.5s computed backoff
