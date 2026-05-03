"""Smoke test for bash_manager — end-to-end without restarting Flask.

Loads DI, instantiates bash_manager, fires a "list folders in Documents"
task with authority_level=99 (so run_shell's approval gate passes),
and prints the result.

Run from repo root:
  .venv/Scripts/python.exe app/assistant/tests/manager_tests/bash_manager/bash_manager_test.py
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../..")))

import time
import app.assistant.tests.test_setup  # noqa: F401 — DI bootstrap
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import (
    Message, ScopeContext, ScopeApprovalPolicy,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def main(task: str, info: str = ""):
    print("initializing system...")
    factory = DI.multi_agent_manager_factory

    t0 = time.time()
    DI.manager_registry.preload_all()
    print(f"preload done in {time.time() - t0:.2f}s")

    print(f"\n=== creating bash_manager ===")
    manager = factory.create_manager("bash_manager")

    request = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content=task,
        task=task,
        information=info,
        scope_context=ScopeContext(
            scope_id="scope::test::bash_manager_smoke",
            owner_id="jukka",
            actor_id="bash_manager_smoke_test",
            surface="test",
            # IMPORTANT: run_shell has approval_min_authority=99.
            # Without authority 99 set on the scope, the tool refuses.
            approval=ScopeApprovalPolicy(authority_level=99),
        ),
    )
    print(f"\n=== invoking bash_manager with task={task!r} ===\n")
    result = manager.request_handler(request)
    print("\n=== result ===")
    print(result)


if __name__ == "__main__":
    main(
        task="List the top-level folder names in my Documents folder.",
        info="",
    )
