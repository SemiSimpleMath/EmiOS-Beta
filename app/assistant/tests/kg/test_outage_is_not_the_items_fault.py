"""An outage stops the run but is not the item's fault, nor the routine's (owner, 2026-10-06).

"most likely reason for failure is credit outage, second most likely is api outage ... if its the first its not very
likely to be resolved before the morning. the second one could be minutes to hours." Until then a credit outage, at a
minute's retry, set every item of a KG queue aside within three minutes and disabled its routine. Pins:
llm_resilience.is_outage reads types and status codes along the cause chain, never message digits; the failure ledger
does not count an outage; the routine manager backs off on one without counting it toward auto-disable; the KG
pipeline's halt belongs to its own run and says whether it was an outage. Uses the kg conftest (isolated test DB).
"""
from datetime import datetime, timezone

from app.assistant.kg import failure_ledger
from app.assistant.routine_manager.routine_manager import RoutineRunState, _set_finished_state
from app.services.llm_resilience import BillingQuotaExhausted, TransientRateLimit, is_outage


class QuotaExhaustedError(Exception):
    """Shaped like llm_client.QuotaExhaustedError, which the breaker raises."""


class APIConnectionError(Exception):
    """Shaped like openai.APIConnectionError."""


class _Status(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def _wrapped(inner):
    try:
        try:
            raise inner
        except Exception as e:
            raise RuntimeError(f"LLM structured_output failed: {e}") from e
    except RuntimeError as outer:
        return outer


def test_an_outage_is_read_by_type_and_status_along_the_chain():
    for exc in (QuotaExhaustedError("no credits"), BillingQuotaExhausted("openai"), TransientRateLimit("rpm"),
                TimeoutError("slow"), ConnectionError("reset"), _Status(503), _Status(429)):
        assert is_outage(exc) and is_outage(_wrapped(exc)), exc
    assert is_outage(_wrapped(APIConnectionError("down")))
    # A work error with status-like digits in an id is not an outage.
    assert not is_outage(RuntimeError("node_merger chose to merge into 'd62686dd-3881-4da9-8dff-83a54db80c7d' (503)"))
    assert not is_outage(_wrapped(ValueError("invalid answer")))
    assert not is_outage(_Status(400))


def test_the_ledger_does_not_count_an_outage_against_the_item():
    assert failure_ledger.record_failure("kg_proposal", "p1", _wrapped(QuotaExhaustedError("no credits"))) is False
    assert failure_ledger.record_failure("kg_proposal", "p1", _wrapped(QuotaExhaustedError("no credits"))) is False
    assert failure_ledger.record_failure("kg_proposal", "p1", _wrapped(APIConnectionError("down"))) is False
    assert "p1" not in failure_ledger.set_aside_ids("kg_proposal")
    for _ in range(failure_ledger.SET_ASIDE_AFTER - 1):
        failure_ledger.record_failure("kg_proposal", "p1", RuntimeError("not a candidate"), label="p1")
    assert "p1" not in failure_ledger.set_aside_ids("kg_proposal"), "outages did not count toward the item"


def test_an_outage_backs_off_without_counting_toward_auto_disable():
    entry = RoutineRunState()
    on_error = {"backoff_base_seconds": 60, "backoff_max_seconds": 3600}
    now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    for n in range(1, 6):
        _set_finished_state(entry, finished_at_utc_iso=now.isoformat(), duration_s=1.0, status="error", error="down",
                            increment_run_count=False, on_error=on_error, finished_at_utc=now, outage=True)
        assert (entry.consecutive_failures, entry.consecutive_outages) == (0, n)
    assert entry.next_attempt_after_utc == (now.replace(minute=16)).isoformat(), "60 s doubled four times: 16 min"
    _set_finished_state(entry, finished_at_utc_iso=now.isoformat(), duration_s=1.0, status="error", error="bad",
                        increment_run_count=False, on_error=on_error, finished_at_utc=now)
    assert (entry.consecutive_failures, entry.consecutive_outages) == (1, 5)
    _set_finished_state(entry, finished_at_utc_iso=now.isoformat(), duration_s=1.0, status="success", error=None,
                        increment_run_count=True, on_error=on_error, finished_at_utc=now)
    assert (entry.consecutive_failures, entry.consecutive_outages, entry.next_attempt_after_utc) == (0, 0, None)


def test_the_kg_pipeline_halt_belongs_to_its_own_run():
    from app.assistant.pipelines.kg_pipeline.runner import KGPipelineRunner

    class Step:
        name = "extract"
        fail = True

        def run_one_cycle(self, ctx):
            if Step.fail:
                raise _wrapped(QuotaExhaustedError("no credits"))
            return 0

    runner = KGPipelineRunner(steps=[Step()], poll_interval=0.01)
    first = runner.run(object(), max_idle_rounds=1, monitor_interval=0.05)
    assert first.status == "halted" and first.halt_outage is True
    Step.fail = False
    second = runner.run(object(), max_idle_rounds=1, monitor_interval=0.05)
    assert second.status != "halted" and second.halt_reason is None and second.halt_outage is False
