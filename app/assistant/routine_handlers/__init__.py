"""Filesystem-discovered routine handlers.

Drop a new handler file in this directory:

    # app/assistant/routine_handlers/my_handler.py
    from app.assistant.routine_handlers import routine_handler

    @routine_handler()
    def my_handler(*, target_date=None, routine=None):
        ...

The handler is auto-registered at startup as a routine function with
the name `my_handler` (or `name="custom"` to override). No edit to
`routine_functions.py` needed.

Why a decorator instead of "register all public functions": explicit
opt-in. A module can import helpers + define internal utilities without
accidentally exposing them as routines.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def routine_handler(name: Optional[str] = None) -> Callable:
    """Decorator marking a function as a discoverable routine handler.

    Pass `name="custom_name"` to register under a name different from
    the function's own __name__.
    """
    def _wrap(fn: Callable) -> Callable:
        fn._routine_handler_name = name or fn.__name__  # type: ignore[attr-defined]
        return fn
    return _wrap


def discover_handlers() -> Dict[str, Callable]:
    """Walk this package, import each module, collect decorated handlers.

    Best-effort: a module that fails to import logs a warning and is
    skipped rather than blowing up startup.
    """
    out: Dict[str, Callable] = {}
    pkg_dir = Path(__file__).resolve().parent
    for module_info in pkgutil.iter_modules([str(pkg_dir)]):
        if module_info.name.startswith("_"):
            continue
        full_name = f"{__name__}.{module_info.name}"
        try:
            mod = importlib.import_module(full_name)
        except Exception as e:
            logger.warning("[routine_handlers] failed to import %s: %s", full_name, e)
            continue
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            attr = getattr(mod, attr_name, None)
            if not callable(attr):
                continue
            handler_name = getattr(attr, "_routine_handler_name", None)
            if not handler_name:
                continue
            if handler_name in out:
                logger.warning(
                    "[routine_handlers] duplicate handler name %r (in %s); keeping first",
                    handler_name, full_name,
                )
                continue
            out[handler_name] = attr
    if out:
        logger.info(
            "[routine_handlers] discovered %d handler(s): %s",
            len(out), ", ".join(sorted(out.keys())),
        )
    return out
