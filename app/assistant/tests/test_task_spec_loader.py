import sys
from pathlib import Path

# Ensure repo root on sys.path.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.utils.task_spec_loader import load_task_spec


def test_load_task_spec_basic():
    spec = load_task_spec("tasks/timesheet/task_spec.md")
    assert spec.task_id == "timesheet_narratives_v1"
    assert spec.manager == "emi_team_manager"
    assert "timesheet log" in (spec.description or "")
    assert "Read the dates" in spec.task_body
    assert "tasks/timesheet/formatting_rules.md" in spec.includes
    assert "tasks/timesheet/sample_timesheet.txt" in spec.allowed_read_files
    assert "tasks/timesheet/sample_timesheet_narratives.txt" in spec.allowed_write_files
    assert "Time Entry Narrative Style Guide" in spec.task_includes


def main() -> int:
    test_load_task_spec_basic()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
