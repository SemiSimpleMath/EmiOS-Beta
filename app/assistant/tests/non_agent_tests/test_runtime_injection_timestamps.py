"""Guard: mid-task @-messages reach the agent timestamped, attributed, ordered.

A message delivered to a running agent via the mailbox (agent_inject) must keep
its arrival time + sender, and the prompt must render those mid-task messages in
time order with explicit precedence (later supersedes earlier; user outranks
system; newest user message is final) and the original dispatch time as baseline.
Times are LOCAL and use the same formatter as chat history.

The baseline is the manager INVOCATION's start (``_invocation_started_utc``,
stamped on the blackboard by ManagerInvoker). It used to come from the
activating Message's timestamp — the CURRENT cycle — so on a long task the
baseline drifted forward and mid-task steering rendered as if it had arrived
BEFORE the task it was meant to supersede (2026-09-11: an invocation started
09:28, the header claimed 09:30, and the user's 09:29/09:30 steers both read
as stale).

Hermetic — fake blackboard/agent, no DB. Part of the pre-push guard.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.utils.time_utils import format_history_local


class _BB:
    def __init__(self):
        self.d = {}

    def get_state_value(self, k):
        return self.d.get(k)

    def update_state_value(self, k, v):
        self.d[k] = v


class _Agent:
    def __init__(self, name, bb):
        self.name = name
        self.blackboard = bb


def test_format_history_local_today_vs_older():
    now = datetime.now(timezone.utc)
    today = format_history_local(now)
    older = format_history_local(now - timedelta(days=3))
    assert len(today) == 5 and today.count(":") == 1            # HH:MM
    assert len(older) == 16 and older.count("-") == 2           # YYYY-MM-DD HH:MM


def test_mailbox_injection_keeps_timestamp_and_sender():
    from app.assistant.manager_runtime.mailbox import (
        MailboxDispatcher, _RUNTIME_INJECTIONS_BB_KEY,
    )
    bb = _BB()
    ts = datetime(2026, 6, 8, 17, 30, tzinfo=timezone.utc)
    MailboxDispatcher._append_runtime_injection(
        bb, "web::planner", "check the new URL", posted_at_utc=ts, from_who="user",
    )
    slot = bb.get_state_value(_RUNTIME_INJECTIONS_BB_KEY)["web::planner"]
    assert len(slot) == 1
    e = slot[0]
    assert e["text"] == "check the new URL"
    assert e["from_who"] == "user"
    assert e["posted_at_utc"] == ts.isoformat()                 # not dropped anymore


def test_renderer_timeordered_with_precedence():
    from app.assistant.agent_runtime.services.prompt_builder import PromptBuilder
    from app.assistant.manager_runtime.mailbox import _RUNTIME_INJECTIONS_BB_KEY

    bb = _BB()
    t1 = datetime(2026, 6, 8, 17, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 8, 17, 30, tzinfo=timezone.utc)
    bb.update_state_value(_RUNTIME_INJECTIONS_BB_KEY, {"a": [
        {"text": "first instruction", "posted_at_utc": t1.isoformat(), "from_who": "user"},
        {"text": "second, overrides the first", "posted_at_utc": t2.isoformat(), "from_who": "user"},
    ]})
    bb.update_state_value(
        "_invocation_started_utc",
        datetime(2026, 6, 8, 16, 0, tzinfo=timezone.utc).isoformat(),
    )
    agent = _Agent("a", bb)

    out = PromptBuilder()._append_runtime_injections(agent, "BASE PROMPT")

    assert "BASE PROMPT" in out                                 # original prompt preserved
    assert "Out-of-band messages" in out
    assert "SUPERSEDES" in out and "most recent user message as final" in out
    assert "Original task dispatched:" in out                   # dispatch-time baseline
    assert "first instruction" in out and "second, overrides the first" in out
    assert "user: " in out                                      # sender attribution
    assert format_history_local(t1.isoformat()) in out          # local timestamp, history format
    # time order preserved (older before newer)
    assert out.index("first instruction") < out.index("second, overrides the first")


def test_baseline_is_the_invocation_start_not_the_current_cycle():
    """The regression: steering must read as arriving AFTER the dispatch."""
    from app.assistant.agent_runtime.services.prompt_builder import PromptBuilder
    from app.assistant.manager_runtime.mailbox import _RUNTIME_INJECTIONS_BB_KEY

    started = datetime(2026, 6, 8, 16, 28, tzinfo=timezone.utc)
    steered = datetime(2026, 6, 8, 16, 29, tzinfo=timezone.utc)
    bb = _BB()
    bb.update_state_value(_RUNTIME_INJECTIONS_BB_KEY, {"a": [
        {"text": "use the docs tool", "posted_at_utc": steered.isoformat(), "from_who": "user"},
    ]})
    bb.update_state_value("_invocation_started_utc", started.isoformat())

    out = PromptBuilder()._append_runtime_injections(_Agent("a", bb), "BASE PROMPT")

    dispatch_line = next(l for l in out.splitlines() if "Original task dispatched" in l)
    steer_line = next(l for l in out.splitlines() if "use the docs tool" in l)
    assert format_history_local(started.isoformat()) in dispatch_line
    assert format_history_local(steered.isoformat()) in steer_line


def test_missing_stamp_omits_the_baseline_rather_than_inventing_one():
    from app.assistant.agent_runtime.services.prompt_builder import PromptBuilder
    from app.assistant.manager_runtime.mailbox import _RUNTIME_INJECTIONS_BB_KEY

    bb = _BB()
    bb.update_state_value(_RUNTIME_INJECTIONS_BB_KEY, {"a": [{"text": "steer", "from_who": "user"}]})
    out = PromptBuilder()._append_runtime_injections(_Agent("a", bb), "BASE PROMPT")
    assert "Original task dispatched" not in out
    assert "steer" in out


def test_renderer_no_injections_is_passthrough():
    from app.assistant.agent_runtime.services.prompt_builder import PromptBuilder
    agent = _Agent("a", _BB())
    out = PromptBuilder()._append_runtime_injections(agent, "BASE")
    assert out == "BASE"                                        # untouched fast path


def test_renderer_handles_legacy_bare_string():
    from app.assistant.agent_runtime.services.prompt_builder import PromptBuilder
    from app.assistant.manager_runtime.mailbox import _RUNTIME_INJECTIONS_BB_KEY
    bb = _BB()
    bb.update_state_value(_RUNTIME_INJECTIONS_BB_KEY, {"a": ["+++ legacy framed instruction +++"]})
    agent = _Agent("a", bb)
    out = PromptBuilder()._append_runtime_injections(agent, "BASE")
    assert "legacy framed instruction" in out                   # back-compat: still rendered
