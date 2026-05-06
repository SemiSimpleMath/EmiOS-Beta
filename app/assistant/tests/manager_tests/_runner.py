"""Generic manager test runner.

One bootstrap + invocation path that any manager test can call. Per-manager
test files become a 5-line wrapper with the task/info specifics.

Usage:
    # As a CLI:
    .venv/Scripts/python.exe -m app.assistant.tests.manager_tests._runner \
        --manager personal_admin_manager --task "Send a test email to Jukka"

    # From another test file:
    from app.assistant.tests.manager_tests._runner import run_manager_test
    run_manager_test(
        manager_type="personal_admin_manager",
        task="Send a test email to Jukka",
        info="Recipient: ...",
    )
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Bootstrap repo on sys.path so this file works as a script OR via -m.
_THIS = os.path.abspath(__file__)
_REPO = os.path.abspath(os.path.join(os.path.dirname(_THIS), "../../../.."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import app.assistant.tests.test_setup  # noqa: F401  — bootstraps DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeContext

logger = get_logger(__name__)


def _install_fast_path_counter() -> dict:
    """Count tool_args fast-path hits vs args-agent invocations during a run.

    Returns a dict the caller can read after request_handler returns:
        {"fast_path_hits": int, "args_agent_runs": int}

    Implementation: the project's ``get_logger`` sets ``propagate=False`` on
    every named logger, so a root-logger handler sees nothing. We attach to
    the specific module loggers we care about: Planner emits the fast-path
    line, llm_client emits the args-agent MODEL_OUTPUT line.
    """
    counts = {"fast_path_hits": 0, "args_agent_runs": 0}

    import logging as _logging

    class _Tally(_logging.Handler):
        def emit(self, record):
            try:
                msg = record.getMessage()
            except Exception:
                return
            if "tool_args fast path" in msg:
                counts["fast_path_hits"] += 1
            elif "shared::tool_arguments" in msg and "MODEL_OUTPUT" in str(getattr(record, "levelname", "")):
                counts["args_agent_runs"] += 1
            elif "shared::tool_arguments" in msg and "LLM RESULT" in msg:
                counts["args_agent_runs"] += 1

    handler = _Tally()
    handler.setLevel(_logging.DEBUG)
    # The fast-path log line fires from the Planner module logger.
    _logging.getLogger("app.assistant.agent_classes.Planner").addHandler(handler)
    # The args-agent MODEL_OUTPUT line fires from the llm_client module logger.
    _logging.getLogger("app.assistant.agent_runtime.services.llm_client").addHandler(handler)
    # Belt & suspenders — Agent base class emits some MODEL_OUTPUT lines too.
    _logging.getLogger("app.assistant.agent_classes.Agent").addHandler(handler)
    counts["_handler"] = handler
    return counts


def run_manager_test(
    *,
    manager_type: str,
    task: str,
    info: str | None = None,
    visible_tools: list[str] | None = None,
    actor_id: str = "manager_test",
    owner_id: str = "jukka",
    surface: str = "test",
    print_result: bool = True,
) -> object:
    """Bootstrap, create a manager, run one task, print + return the result.

    Returns whatever ``manager.request_handler(message)`` returns (typically
    a ToolResult). Also prints fast-path hit count vs args-agent run count
    so a test can see at a glance whether the JSON-args path is firing.
    """
    print(f"\n{'='*60}")
    print(f"Manager test: {manager_type}")
    print(f"Task: {task!r}")
    if info:
        print(f"Info: {info!r}")
    print(f"{'='*60}\n")

    factory = DI.multi_agent_manager_factory
    preload_start = time.time()
    DI.manager_registry.preload_all()
    print(f"[preload] {time.time() - preload_start:.2f}s")

    counts = _install_fast_path_counter()

    print(f"[create] {manager_type}")
    manager = factory.create_manager(manager_type)

    scope_id = f"scope::test::{manager_type}::{int(time.time())}"
    msg = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content=task,
        task=task,
        information=info,
        scope_context=ScopeContext(
            scope_id=scope_id,
            owner_id=owner_id,
            actor_id=actor_id,
            surface=surface,
        ),
        data={"visible_tools": visible_tools or []},
    )

    started = time.time()
    result = manager.request_handler(msg)
    elapsed = time.time() - started

    print(f"\n[run] {elapsed:.2f}s wall")
    print(f"[fast-path] hits={counts['fast_path_hits']} args_agent_runs={counts['args_agent_runs']}")

    if print_result:
        print("\n----- RESULT -----")
        print(result)
        print("------------------\n")

    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a single manager test")
    parser.add_argument("--manager", required=True, help="Manager type (e.g. personal_admin_manager)")
    parser.add_argument("--task", required=True, help="Task string")
    parser.add_argument("--info", default=None, help="Information string (optional)")
    parser.add_argument(
        "--tools", default="",
        help="Comma-separated list of visible_tools (passed in Message.data['visible_tools'])",
    )
    parser.add_argument("--actor-id", default="manager_test")
    parser.add_argument("--owner-id", default="jukka")
    parser.add_argument("--surface", default="test")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    visible_tools = [t.strip() for t in (args.tools or "").split(",") if t.strip()]
    run_manager_test(
        manager_type=args.manager,
        task=args.task,
        info=args.info,
        visible_tools=visible_tools,
        actor_id=args.actor_id,
        owner_id=args.owner_id,
        surface=args.surface,
    )


if __name__ == "__main__":
    main()
