from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from app.assistant.utils.logging_config import get_logger

from .types import RoutineLike
from app.assistant.routine_manager.run_types import RoutineRunContext, RoutineRunResult

logger = get_logger(__name__)


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
            fn = self._rediscover(fn_name)
        if fn is None:
            raise ValueError(
                f"Unknown routine function: {fn_name}. The registry is built at "
                "boot (routine_functions + routine_handlers discovery) and was "
                "re-checked just now; check the boot log for '[routine_handlers]' "
                "import failures."
            )

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
        result = fn(**kwargs)
        # A function that REPORTS failure counts like one that raises.
        # (sleep_camera_tick_local-style handlers return {"status": "error",
        # …} dicts; before this, those were invisible to the failure
        # machinery — the runner hardcoded success.)
        if isinstance(result, dict) and str(result.get("status") or "").strip().lower() == "error":
            return RoutineRunResult(
                status="error",
                message=str(
                    result.get("error") or result.get("message")
                    or "function reported status=error"
                ),
                data={"function_name": fn_name, "result": result},
            )
        return RoutineRunResult(status="success", data={"function_name": fn_name})

    def _rediscover(self, fn_name: str) -> Optional[Callable[..., Any]]:
        """A handler file dropped after boot isn't in the import-frozen
        registry even though its routine config hot-reloads every tick
        (the pod_retention incident, routine audit R3). One fresh
        discovery pass picks it up; boot-registered names keep priority.
        """
        try:
            from app.assistant.routine_handlers import discover_handlers
            for name, fn in discover_handlers().items():
                self._registry.setdefault(name, fn)
        except Exception:
            logger.warning(
                "[function_runner] handler re-discovery failed", exc_info=True,
            )
        return self._registry.get(fn_name)


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
