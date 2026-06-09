import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../..")))

import time
import app.assistant.tests.test_setup  # noqa: F401 — bootstraps DI
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message, ScopeContext, ScopeApprovalPolicy
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def main(task, info=None):
    factory = DI.multi_agent_manager_factory
    DI.manager_registry.preload_all()

    manager = factory.create_manager("web_manager")
    request_message = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content=task,
        task=task,
        information=info,
        scope_context=ScopeContext(
            scope_id="scope::test::web_manager_poc",
            owner_id="alex",
            actor_id="web_manager_poc_test",
            surface="test",
            approval=ScopeApprovalPolicy(authority_level=99),  # full agent authority (clears tool floors)
        ),
        # The harness is the ingress layer: it must supply the precomputed visible tool list.
        # scope_adapter only CONSTRAINS this against allowed_tools (default "all" → all pass).
        data={"visible_tools": [
            "search_web", "scrape_url", "find_tool", "install_tool", "http_request", "ask_user",
            "read_tool_result", "pod_fetch", "pod_search",   # re-read offloaded results by id
        ]},
    )
    started = time.perf_counter()
    result = manager.request_handler(request_message)
    print(f"\n⏱️ web_manager took {(time.perf_counter() - started):.1f} s\n")
    print(result)

    # Keep a full, clean transcript of the planner's + summarizer's rendered user.j2 + outputs.
    from app.assistant.tests.manager_tests.web_manager.transcript_extractor import extract_for_current_run
    extract_for_current_run()


if __name__ == "__main__":
    # A multi-finding research query: several providers => the planner should pod each as a finding.
    task = (
        "Find a few reputable HVAC / air-conditioning service providers in or near Irvine, CA. "
        "For each, return the company name, phone number, and website. Return 2-4 options."
    )
    main(task)
