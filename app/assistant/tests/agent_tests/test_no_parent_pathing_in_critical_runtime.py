from __future__ import annotations

import re
from pathlib import Path

from app.assistant.utils.path_utils import get_repo_root


_PARENTS_INDEX_PATTERN = re.compile(r"\.parents\[\d+\]")

_CRITICAL_FILES = [
    "app/assistant/control_nodes/task_compile_metadata_node.py",
    "app/assistant/control_nodes/task_compile_post_node.py",
    "app/assistant/lib/tools/run_task/run_task.py",
    "app/assistant/routine_manager/runners/taskrunner/task_runner.py",
    "app/assistant/utils/task_spec_loader.py",
    "app/assistant/utils/task_registry.py",
    "app/assistant/utils/routine_registry.py",
    "app/assistant/utils/job_spec_loader.py",
    "app/assistant/lib/tool_registry/tool_registry.py",
    "app/assistant/lib/tool_registry/mcp_server_directory.py",
    "app/assistant/lib/tool_registry/mcp_tool_cache.py",
    "app/assistant/lib/tool_registry/mcp_install_registry.py",
    "app/assistant/lib/mcp/tool_runner.py",
    "app/assistant/slack_interface/slack_room_config.py",
    "app/assistant/control_nodes/tool_result_handler.py",
    "app/assistant/room_session_manager/room_session_manager.py",
    "app/assistant/agent_registry/agent_registry.py",
    "app/assistant/pipelines/dayflow/utils/context_sources.py",
    "app/assistant/pipelines/dayflow/activity/activity_recorder.py",
    "app/assistant/lib/core_tools/slack/slack.py",
    "app/assistant/agent_classes/ToolArgumentsPlaywright.py",
    "app/assistant/lib/google_auth/google_credentials.py",
    "app/assistant/dj_manager/music_dataset.py",
    "app/assistant/agent_flow_manager/agent_flow_manager.py",
]


def test_critical_runtime_files_do_not_use_parent_index_pathing() -> None:
    repo_root = get_repo_root()
    offending: list[str] = []
    for rel_path in _CRITICAL_FILES:
        file_path = (repo_root / rel_path).resolve()
        text = file_path.read_text(encoding="utf-8")
        if _PARENTS_INDEX_PATTERN.search(text):
            offending.append(rel_path)

    assert not offending, (
        "Found fragile Path.parents[N] pathing in critical runtime files. "
        "Use app.assistant.utils.path_utils helpers instead:\n- " + "\n- ".join(offending)
    )


def test_non_test_assistant_modules_avoid_parent_index_pathing() -> None:
    repo_root = get_repo_root()
    assistant_root = (repo_root / "app" / "assistant").resolve()
    offending: list[str] = []

    for file_path in assistant_root.rglob("*.py"):
        rel = file_path.relative_to(repo_root).as_posix()

        # Skip tests and archived folders.
        if "/test/" in rel or "/tests/" in rel or rel.endswith("/test.py"):
            continue
        if ".archive/" in rel:
            continue
        # The resolver implementation itself is allowed to use fixed parent math.
        if rel == "app/assistant/utils/path_utils.py":
            continue

        text = file_path.read_text(encoding="utf-8")
        if _PARENTS_INDEX_PATTERN.search(text):
            offending.append(rel)

    assert not offending, (
        "Found Path.parents[N] usage in non-test assistant modules:\n- " + "\n- ".join(sorted(offending))
    )
