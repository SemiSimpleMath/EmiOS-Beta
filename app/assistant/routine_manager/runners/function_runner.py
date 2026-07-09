from __future__ import annotations

import inspect
from typing import Any, Callable

from .types import RoutineLike
from app.assistant.routine_manager.run_types import RoutineRunContext, RoutineRunResult


class FunctionRoutineRunner:
    """
    Thin wrapper around a function registry.

    The registry is passed in so RoutineManager stays boring and domain logic
    stays out of the scheduler.
    """

    def __init__(self, registry: dict[str, Callable[..., Any]]):
        self._registry = registry

    def run(self, routine: RoutineLike, run_ctx: RoutineRunContext) -> RoutineRunResult:
        spec = routine.spec or {}
        fn_name = str(spec.get("function_name") or "").strip()
        if not fn_name:
            raise ValueError("function runner requires spec.function_name")
        fn = self._registry.get(fn_name)
        if fn is None:
            raise ValueError(f"Unknown routine function: {fn_name}")

        # Bind ONCE from the function's declared signature, call ONCE.
        # (The previous dispatch probed four call shapes catching TypeError
        # between them — which also caught TypeErrors raised INSIDE the
        # function body and re-invoked the function with narrower args,
        # duplicating its side effects. A body TypeError now propagates as
        # the real failure it is.)
        kwargs = _kwargs_for(fn, {
            "target_date": run_ctx.target_date,
            "routine": routine,
            "event_message": run_ctx.event_message,
        })
        if run_ctx.event_message is not None and "event_message" not in kwargs:
            # An event-triggered fire carrying a payload into a handler that
            # can't receive it is a wiring error, not a quiet degradation.
            raise ValueError(
                f"Routine function {fn_name!r} does not accept event_message but "
                "was fired by an event trigger; add event_message=None to its "
                "signature."
            )
        fn(**kwargs)
        return RoutineRunResult(status="success", data={"function_name": fn_name})


def _kwargs_for(fn: Callable[..., Any], available: dict[str, Any]) -> dict[str, Any]:
    """The subset of ``available`` the function's signature can receive by
    keyword. A ``**kwargs`` parameter accepts everything."""
    params = inspect.signature(fn).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(available)
    accepted = {
        name for name, p in params.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return {k: v for k, v in available.items() if k in accepted}
