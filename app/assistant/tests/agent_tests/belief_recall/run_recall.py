"""What chat_gate would be shown for a message: the beliefs recalled from the shadow store. READ-ONLY
(no surfacing log). One or more messages; names in the message lift beliefs about those people.

    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_recall.run_recall "what should we do for dinner" "is katy home tonight"
"""
from __future__ import annotations

import argparse
import sys

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from belief_engine.intake import recall


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("messages", nargs="+")
    parser.add_argument("-k", type=int, default=10)
    args = parser.parse_args()
    from app.assistant.ServiceLocator.service_locator import DI
    from belief_engine.intake import agents
    user_data = DI.resource_manager.get_resource(scope_context=agents.scope(), resource_id="resource_user_data") or {}
    people = user_data.get("important_people") or []
    print(f"{len(recall.beliefs())} beliefs in the store")
    for msg in args.messages:
        names = [p["name"] for p in people if p.get("name") and p["name"].lower() in msg.lower()]
        print("=" * 100)
        print(f"MESSAGE: {msg}   names: {names}")
        for i, b in enumerate(recall.recall(msg, names=names, k=args.k), 1):
            print(f"  {i:2}. score {b['score']:.3f} cos {b['relevance']:.3f} [{b['kind']}] {b['statement']} (last seen {b['last_day']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
