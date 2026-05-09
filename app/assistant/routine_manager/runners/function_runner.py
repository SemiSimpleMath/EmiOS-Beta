from __future__ import annotations

from typing import Any, Callable, Optional

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

        # Prefer keyword invocation. Newer event-trigger handlers accept
        # event_message; fall back to older signatures for backward compat.
        try:
            fn(
                target_date=run_ctx.target_date,
                routine=routine,
                event_message=run_ctx.event_message,
            )
        except TypeError:
            try:
                fn(target_date=run_ctx.target_date, routine=routine)
            except TypeError:
                try:
                    fn(run_ctx.target_date)
                except TypeError:
                    fn()
        return RoutineRunResult(status="success", data={"function_name": fn_name})
