import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../..")))

import time
import json
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


def main():
    print("Building situation snapshot...")
    from app.assistant.pipelines.dayflow.utils.situation_snapshot import build_situation_snapshot
    snapshot = build_situation_snapshot()
    print(f"Snapshot: {len(snapshot)} chars")
    print(snapshot[:500])
    print("...\n")

    print("Running situation audit...")
    start = time.time()
    from app.assistant.pipelines.dayflow.utils.situation_audit_runner import run_situation_audit
    result = run_situation_audit()
    elapsed = time.time() - start

    print(f"\n{'='*60}")
    print(f"Completed in {elapsed:.1f}s")
    print(f"{'='*60}")
    print(json.dumps(result, indent=2, default=str))

    # Also show full agent output if available
    from app.assistant.pipelines.dayflow.utils.situation_snapshot import build_situation_snapshot
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
    from app.assistant.utils.pydantic_classes import Message

    scope_context = build_pipeline_scope_context(
        pipeline_id="dayflow", actor_id="test", room_id="dayflow_orchestrator", room_surface="ui",
    )
    agent = DI.agent_factory.create_agent("situation_auditor")
    r2 = agent.action_handler(Message(
        agent_input={"task": snapshot, "information": ""},
        scope_context=scope_context,
    ))
    if hasattr(r2, "data") and isinstance(r2.data, dict):
        print("\n=== Full Agent Output ===")
        print(json.dumps(r2.data, indent=2, default=str)[:3000])


if __name__ == "__main__":
    main()
