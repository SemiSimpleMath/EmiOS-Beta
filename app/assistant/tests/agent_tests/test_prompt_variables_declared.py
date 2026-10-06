"""Every variable an agent's prompt uses is a context item its config declares.

The boot validator (validation/agent_validator._check_undeclared_template_vars) only logs this, and the log was
missed: subconscious::concern_tasks used {{ information }} (the concern's entries on the brain's list) without
declaring it, so from 2026-10-05 its prompt said "(nothing yet)" and it could not modify or cancel an entry by id.
This turns the warning into a failing test over the real agent registry.
"""
import logging

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.validation import agent_validator


def test_no_prompt_uses_an_undeclared_variable(caplog):
    with caplog.at_level(logging.WARNING, logger=agent_validator.logger.name):
        agent_validator._check_undeclared_template_vars(DI.agent_registry)
    missing = [r.getMessage() for r in caplog.records if "does NOT declare it" in r.getMessage()]
    assert missing == []
