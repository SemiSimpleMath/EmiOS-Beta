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

import time
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


class TestCircuitBreaker:
    """Billing exhaustion trips the breaker and RAISES. It must never kill the
    process: under `restart: unless-stopped` a hard exit becomes a crash loop —
    the container returns, calls the same exhausted provider, and dies again
    (observed at ~20s intervals), taking the UI and every non-LLM feature with
    it. The breaker is what stops batch loops completing empty: once tripped,
    _guard_quota() raises before any further call is attempted."""

    def test_billing_trips_breaker_and_returns_true(self, monkeypatch, _breaker_reset):
        exits = []
        monkeypatch.setattr(lc.os, "_exit", lambda code: exits.append(code))
        out = lc._check_and_trip_quota("openai", BillingQuotaExhausted(provider="openai"))
        assert out is True
        assert "openai" in lc._quota_tripped
        assert exits == [], "billing exhaustion must not terminate the process"

    def test_rate_window_never_trips_the_breaker(self, monkeypatch, _breaker_reset):
        exits = []
        monkeypatch.setattr(lc.os, "_exit", lambda code: exits.append(code))
        out = lc._check_and_trip_quota(
            "gemini", TransientRateLimit("429 per minute", retry_after=12),
        )
        assert out is False
        assert exits == []
        assert not lc._quota_tripped

    def test_unstructured_billing_string_still_trips(self, monkeypatch, _breaker_reset):
        exits = []
        monkeypatch.setattr(lc.os, "_exit", lambda code: exits.append(code))
        out = lc._check_and_trip_quota(
            "openai",
            Exception("insufficient_quota: you exceeded your current quota"),
        )
        assert out is True
        assert exits == []

    def test_tripped_breaker_short_circuits_later_calls(self, _breaker_reset):
        """After tripping, no further network call is attempted — the guard
        raises first. This is what keeps a KG batch from marking items done."""
        calls = []

        class _Provider:
            provider_name = "openai"

            def structured_output(self, message, **params):
                calls.append(message)
                raise BillingQuotaExhausted(provider="openai")

        iface = lc.LLMInterface(_Provider())

        with pytest.raises(lc.QuotaExhaustedError):
            iface.structured_output("first")
        assert len(calls) == 1

        # Second call must not reach the provider at all.
        with pytest.raises(lc.QuotaExhaustedError):
            iface.structured_output("second")
        assert len(calls) == 1, "breaker did not short-circuit the second call"

    def test_reset_breaker_allows_calls_again(self, _breaker_reset):
        lc._quota_tripped["openai"] = lc._BreakerState()
        lc.reset_quota_breaker("openai")
        assert not lc._quota_tripped.get("openai")


class TestHalfOpenRecovery:
    """An armed breaker must heal itself. Billing exhaustion is temporary — a
    top-up restores service — but nothing in the process re-checks, so the
    breaker used to refuse a WORKING provider until someone restarted. Worse,
    routine auto-recovery probes call through _guard_quota(), so they failed
    against the latch instead of the network and re-disabled their routines on
    every cycle. After the cooldown one trial call goes out; its result decides."""

    def _armed(self, provider="openai", age_s=0.0):
        """Arm the breaker as if it tripped `age_s` seconds ago."""
        state = lc._BreakerState()
        state.armed_at = time.monotonic() - age_s
        lc._quota_tripped[provider] = state
        return state

    def test_inside_cooldown_still_refuses(self, _breaker_reset):
        self._armed(age_s=lc.QUOTA_PROBE_AFTER_S - 5)
        assert lc._quota_refuses_call("openai") is True

    def test_after_cooldown_one_call_is_let_through(self, _breaker_reset):
        self._armed(age_s=lc.QUOTA_PROBE_AFTER_S + 1)
        assert lc._quota_refuses_call("openai") is False, "probe was not elected"
        # Single-flight: everyone else keeps being refused while it is in flight.
        assert lc._quota_refuses_call("openai") is True
        assert lc._quota_refuses_call("openai") is True

    def test_successful_probe_closes_the_breaker(self, _breaker_reset):
        calls = []

        class _Provider:
            provider_name = "openai"

            def structured_output(self, message, **params):
                calls.append(message)
                return "ok"

        self._armed(age_s=lc.QUOTA_PROBE_AFTER_S + 1)
        iface = lc.LLMInterface(_Provider())
        assert iface.structured_output("probe") == "ok"
        assert calls == ["probe"], "the probe must actually reach the provider"
        assert "openai" not in lc._quota_tripped, "success must close the breaker"
        # And normal traffic flows again immediately.
        assert iface.structured_output("next") == "ok"
        assert len(calls) == 2

    def test_failed_probe_rearms_and_restarts_the_cooldown(self, _breaker_reset):
        class _Provider:
            provider_name = "openai"

            def structured_output(self, message, **params):
                raise BillingQuotaExhausted(provider="openai")

        self._armed(age_s=lc.QUOTA_PROBE_AFTER_S + 1)
        iface = lc.LLMInterface(_Provider())
        with pytest.raises(lc.QuotaExhaustedError):
            iface.structured_output("probe")
        state = lc._quota_tripped.get("openai")
        assert state is not None, "still-empty billing must leave the breaker armed"
        assert state.probe_in_flight is False, "probe slot must be released"
        assert lc._quota_refuses_call("openai") is True, "cooldown must restart"

    def test_non_billing_probe_failure_does_not_strand_the_breaker(self, _breaker_reset):
        """A probe that dies on a network blip proves nothing about billing. It
        must release its slot, or no probe is ever elected again and the breaker
        refuses forever."""
        class _Provider:
            provider_name = "openai"

            def structured_output(self, message, **params):
                raise RuntimeError("connection reset")

        state = self._armed(age_s=lc.QUOTA_PROBE_AFTER_S + 1)
        iface = lc.LLMInterface(_Provider())
        # structured_output elects the probe itself via _guard_quota().
        with pytest.raises(RuntimeError):
            iface.structured_output("probe")
        assert state.probe_in_flight is False, "slot leaked — breaker is stranded"
        # A later cooldown elects a fresh probe rather than refusing forever.
        state.armed_at = time.monotonic() - (lc.QUOTA_PROBE_AFTER_S + 1)
        assert lc._quota_refuses_call("openai") is False

    def test_closed_breaker_success_is_cheap_and_harmless(self, _breaker_reset):
        """_note_quota_success runs after every successful call; with no breaker
        armed it must be a no-op and must not create state."""
        lc._note_quota_success("openai")
        assert not lc._quota_tripped


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
