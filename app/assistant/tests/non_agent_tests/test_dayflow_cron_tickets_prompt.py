"""dayflow_cron_tickets user prompt: the day start, the timer wording and the ticket header
render from what the resources actually hold (prompt audit 2026-11-15)."""

from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
from app.assistant.utils.path_utils import get_app_root

_PROMPTS = get_app_root() / "assistant" / "agents" / "dayflow_cron_tickets" / "prompts"


def _activity(*, last, reset, reason, threshold=60):
    return {
        "display_name": "Hydration",
        "threshold_minutes": threshold,
        "last_occurrence_utc": last,
        "last_reset_utc": reset,
        "last_reset_reason": reason,
        "show_daily_count": True,
    }


def _render(*, activity, count=0, sleep=None, responded=None):
    template = _jinja_env.from_string((_PROMPTS / "user.j2").read_text(encoding="utf-8"))
    return template.render(
        date_time="2026-11-15 10:10:00 PST",
        day_of_week="Sunday",
        resource_sleep_output=sleep or {"user_is_up": True, "day_started": False, "day_start_time": None},
        resource_expected_calendar={"date": "2026-11-15", "expected_schedule": []},
        resource_daily_context_generator_output={},
        resource_tracked_activities_output={"activities": {"hydration": activity}},
        time_since={"minutes_since_hydration": 136.0, "hydration_count_today": count},
        active_tickets=[],
        recent_responded_tickets=responded or {},
    )


def test_day_start_renders_the_real_field_and_nothing_before_the_day_starts():
    started = _render(
        activity=_activity(last="a", reset="b", reason="init"),
        sleep={"user_is_up": True, "day_started": True, "day_start_time": "2026-11-15 08:08 AM PST"},
    )
    assert "- Day start: 2026-11-15 08:08 AM PST" in started
    not_started = _render(activity=_activity(last="a", reset="b", reason="init"))
    assert "Day start" not in not_started
    assert "unknown" not in not_started.split("DAILY CONTEXT")[0]


def test_timer_restarted_at_day_start_is_not_called_since_last():
    out = _render(activity=_activity(last="t0", reset="t0", reason="daily_boundary"))
    assert "Hydration: 0 (136.0 min since the day started; timer started then)" in out
    assert "since last" not in out


def test_timer_restarted_on_return_to_computer_says_so():
    out = _render(activity=_activity(last="t0", reset="t0", reason="afk_return"), count=2)
    assert "Hydration: 2 (136.0 min since back at the computer; timer restarted then)" in out


def test_reset_reason_unknown_and_none_recorded_today():
    out = _render(activity=_activity(last="t0", reset="t1", reason="init"))
    assert "Hydration: 0 (136.0 min on the timer; none recorded today)" in out


def test_recorded_occurrence_is_since_last():
    out = _render(activity=_activity(last="t0", reset="t1", reason="init"), count=3)
    assert "Hydration: 3 (136.0 min since last)" in out


def test_ticket_state_header_only_with_content():
    empty = _render(activity=_activity(last="t0", reset="t1", reason="init"))
    assert "TICKET STATE" not in empty
    declined = _render(
        activity=_activity(last="t0", reset="t1", reason="init"),
        responded={"declined": [{"title": "Hydration reminder", "responded_at_local": "10:00 AM"}]},
    )
    assert "TICKET STATE" in declined
    assert "Hydration reminder [Declined at 10:00 AM]" in declined
