"""load_job_spec parses the job frontmatter, loads each task spec and builds the bundle text.

The specs are written by the test: the tasks/timesheet/ specs this test once read
are personal and were untracked in 966f955f (tasks/*/ is gitignored).
"""
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.tests.test_task_spec_loader import write_task_spec
from app.assistant.utils.job_spec_loader import load_job_spec


def test_load_job_spec_basic(tmp_path):
    task_path, _ = write_task_spec(tmp_path)
    job_path = tmp_path / "job_spec.md"
    job_path.write_text(
        "---\n"
        "job_schema_version: 1\n"
        "job_id: fixture_batch_v1\n"
        "description: Batch run for the fixture task.\n"
        "tasks:\n"
        "  - job_id: fixture_summaries\n"
        "    manager: emi_team_manager\n"
        f"    task_file: {task_path.as_posix()}\n"
        "    depends_on: []\n"
        "---\n"
        "\n"
        "Run the fixture summaries task as a single managed job.\n",
        encoding="utf-8",
    )

    spec = load_job_spec(str(job_path))
    assert spec.job_id == "fixture_batch_v1"
    assert "Batch run" in (spec.description or "")
    assert len(spec.tasks) == 1
    job = spec.tasks[0]
    assert job.job_id == "fixture_summaries"
    assert job.manager == "emi_team_manager"
    assert job.task_file == task_path.as_posix()
    assert job.task_spec.task_id == "fixture_task_v1"
    assert "fixture summaries task" in spec.job_bundle_text
    assert "manager_type: emi_team_manager" in spec.job_bundle_text
