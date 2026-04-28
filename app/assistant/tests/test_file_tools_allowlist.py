import sys
from pathlib import Path

# Ensure repo root on sys.path.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.utils.pydantic_classes import ToolMessage
from app.assistant.lib.tools.read_text_file.read_text_file import ReadTextFileTool
from app.assistant.lib.tools.append_text_file.append_text_file import AppendTextFileTool


def test_read_text_file_allowlist_blocks():
    tool = ReadTextFileTool()
    msg = ToolMessage(
        tool_name="read_text_file",
        tool_data={
            "arguments": {"file_path": "tasks/sample_timesheet.txt"},
            "allowed_read_files": ["tasks/other.txt"],
        },
    )
    res = tool.execute(msg)
    assert res.result_type == "error"
    assert "blocked" in (res.content or "")


def test_append_text_file_allowlist_allows():
    tool = AppendTextFileTool()
    msg = ToolMessage(
        tool_name="append_text_file",
        tool_data={
            "arguments": {"file_path": "tasks/allowlist_test_output.txt", "content": "ok"},
            "allowed_write_files": ["tasks/allowlist_test_output.txt"],
        },
    )
    res = tool.execute(msg)
    assert res.result_type == "file_write"


def main() -> int:
    test_read_text_file_allowlist_blocks()
    test_append_text_file_allowlist_allows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
