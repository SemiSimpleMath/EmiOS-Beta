"""
Manual test script for shared::tool_narrower.

Pattern mirrors other script-style agent tests in this repo.
"""

import json

import app.assistant.tests.test_setup  # noqa: F401 (side-effect: initializes DI)

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message


def _candidate_tools_payload() -> str:
    tools = [
        {"name": "append_text_file", "description": "Append text to an existing file."},
        {"name": "find_tool", "description": "Find tools by purpose and return matches."},
        {"name": "install_tool", "description": "Install or expose a tool to runtime visibility."},
        {"name": "run_compiled_task", "description": "Run a named compiled task."},
        {"name": "run_job_spec", "description": "Run a named job specification."},
        {"name": "search_web", "description": "Search the web for current information."},
        {"name": "scrape_url", "description": "Extract webpage content from a URL."},
        {"name": "kg_find_node", "description": "Search knowledge graph nodes by semantic criteria."},
        {"name": "kg_update_node", "description": "Update an existing knowledge graph node."},
        {"name": "kg_create_node", "description": "Create a new node in the knowledge graph."},
        {"name": "get_calendar_events", "description": "List upcoming calendar events."},
        {"name": "read_text_file", "description": "Read text files from the repo by file path."},
        {"name": "create_calendar_event", "description": "Create a calendar event."},
        {"name": "get_todo_tasks", "description": "List todo tasks."},
        {"name": "create_todo_task", "description": "Create a todo task."},
        {"name": "send_email", "description": "Send an email message."},
        {"name": "ask_user", "description": "Ask user for missing clarification."},
        {"name": "write_text_file", "description": "Write text to a file path in the repo."},
        {"name": "web_manager", "description": "Manager for web browsing and web workflows."},
        {"name": "personal_admin_manager", "description": "Manager for calendar/todo/reminders/email workflows."},
    ]
    return json.dumps(tools, indent=2)


def main() -> bool:
    print("=" * 80)
    print("🧪 Testing shared::tool_narrower with timesheet task")
    print("=" * 80)

    agent = DI.agent_factory.create_agent("shared::tool_narrower")
    if not agent:
        print("❌ Agent creation failed")
        return False

    task_short = (
        "Run your own business of choosing for a month."
    )
    information = (
        "This task is completely open ended."
    )

    msg = Message(
        agent_input={
            "task": task_short,
            "information": information,
            "candidate_tools": _candidate_tools_payload(),
        }
    )

    result = agent.action_handler(msg)
    print("\n=== TOOL NARROWER OUTPUT ===")
    print(result)

    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        print("❌ FAIL: missing structured data payload")
        return False

    likely_tools = data.get("likely_tools")
    if not isinstance(likely_tools, list):
        print("❌ FAIL: likely_tools missing or not a list")
        return False

    expected_any = {"read_text_file", "write_text_file"}
    if not expected_any.intersection(set([str(x) for x in likely_tools])):
        print("❌ FAIL: expected at least one of read_text_file/write_text_file in likely_tools")
        return False

    print("✅ PASS")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
