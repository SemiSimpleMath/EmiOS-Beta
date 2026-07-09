"""Function-runner dispatch: bind once from the signature, call once
(routine audit R1, 2026-07-08).

The previous dispatch probed four call shapes catching TypeError between
them — every legacy function paid a raise+catch per run (the chained-
traceback litter), and a TypeError raised INSIDE the function body
re-invoked the function with narrower args, duplicating its side effects.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.assistant.routine_manager.run_types import RoutineRunContext
from app.assistant.routine_manager.runners.function_runner import (
    FunctionRoutineRunner,
    _kwargs_for,
)


def _ctx(event_message=None) -> RoutineRunContext:
    now = datetime(2026, 7, 9, 8, 0, tzinfo=timezone.utc)
    return RoutineRunContext(
        run_id="r1", now_utc=now, now_local=now,
        target_date="2026-07-09", event_message=event_message,
    )


def _routine(fn_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        routine_id="t", runner="function",
        spec={"function_name": fn_name}, notes=None,
    )


def test_body_typeerror_propagates_and_function_runs_once():
    calls = []

    def fn(*, target_date=None, routine=None):
        calls.append(1)
        raise TypeError("bug inside the work")

    with pytest.raises(TypeError, match="bug inside the work"):
        FunctionRoutineRunner({"fn": fn}).run(_routine("fn"), _ctx())
    assert calls == [1]  # the old cascade re-invoked with narrower args


def test_legacy_signature_receives_its_declared_kwargs():
    seen = {}

    def fn(*, target_date=None, routine=None):
        seen.update(target_date=target_date, routine=routine)

    FunctionRoutineRunner({"fn": fn}).run(_routine("fn"), _ctx())
    assert seen["target_date"] == "2026-07-09"
    assert seen["routine"].routine_id == "t"


def test_full_signature_receives_event_message():
    seen = {}

    def fn(*, target_date=None, routine=None, event_message=None):
        seen["event_message"] = event_message

    FunctionRoutineRunner({"fn": fn}).run(_routine("fn"), _ctx(event_message="MSG"))
    assert seen["event_message"] == "MSG"


def test_event_payload_into_handler_without_event_message_raises():
    def fn(*, target_date=None, routine=None):
        pass

    with pytest.raises(ValueError, match="does not accept event_message"):
        FunctionRoutineRunner({"fn": fn}).run(_routine("fn"), _ctx(event_message="MSG"))


def test_zero_arg_and_var_kwargs_functions():
    calls = []

    def bare():
        calls.append("bare")

    seen = {}

    def sponge(**kwargs):
        seen.update(kwargs)

    runner = FunctionRoutineRunner({"bare": bare, "sponge": sponge})
    runner.run(_routine("bare"), _ctx())
    runner.run(_routine("sponge"), _ctx())
    assert calls == ["bare"]
    assert set(seen) == {"target_date", "routine", "event_message"}


def test_registry_census_every_live_function_binds():
    """Every registered routine function (manual + auto-discovered) must
    accept the standard kwargs via signature binding."""
    import inspect

    from app.assistant.routine_manager.routine_functions import ROUTINE_FUNCTION_REGISTRY

    assert ROUTINE_FUNCTION_REGISTRY, "registry empty — import problem?"
    for name, fn in ROUTINE_FUNCTION_REGISTRY.items():
        kwargs = _kwargs_for(fn, {"target_date": None, "routine": None, "event_message": None})
        inspect.signature(fn).bind(**kwargs)  # raises if the subset is wrong
