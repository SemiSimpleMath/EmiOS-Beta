import sys
from pathlib import Path

# Ensure repo root on sys.path.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.job_spec_loader import load_job_spec


def test_load_job_spec_basic():
    spec = load_job_spec("tasks/timesheet/job_spec.md")
    assert spec.job_id == "timesheet_batch_v1"
    assert "Batch run" in (spec.description or "")
    assert len(spec.tasks) == 1
    job = spec.tasks[0]
    assert job.job_id == "timesheet_narratives"
    assert job.manager == "emi_team_manager"
    assert job.task_file == "tasks/timesheet/task_spec.md"
    assert "timesheet narratives" in (spec.job_bundle_text or "")
    assert "manager_type: emi_team_manager" in spec.job_bundle_text


def main() -> int:
    test_load_job_spec_basic()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
