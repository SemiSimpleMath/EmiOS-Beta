"""load_task_spec parses frontmatter, file includes and input/output paths.

The spec is written by the test: the tasks/timesheet/ specs this test once read
are personal and were untracked in 966f955f (tasks/*/ is gitignored).
"""
import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.utils.task_spec_loader import load_task_spec


def write_task_spec(tmp_path):
    rules = tmp_path / "formatting_rules.md"
    rules.write_text("# Fixture Style Guide\nOne line per entry.\n", encoding="utf-8")
    spec = tmp_path / "task_spec.md"
    spec.write_text(
        "---\n"
        "schema_version: 1\n"
        "task_id: fixture_task_v1\n"
        "manager: emi_team_manager\n"
        "description: Summarize a fixture log.\n"
        "includes:\n"
        f"  - {rules.as_posix()}\n"
        "inputs:\n"
        "  - id: log\n"
        "    path: tasks/fixture/input.txt\n"
        "outputs:\n"
        "  - id: summary\n"
        "    path: tasks/fixture/output.txt\n"
        "---\n"
        "\n"
        "Read the entries and write one summary line each.\n",
        encoding="utf-8",
    )
    return spec, rules


def test_load_task_spec_basic(tmp_path):
    spec_path, rules = write_task_spec(tmp_path)
    spec = load_task_spec(str(spec_path))
    assert spec.task_id == "fixture_task_v1"
    assert spec.manager == "emi_team_manager"
    assert spec.description == "Summarize a fixture log."
    assert "Read the entries" in spec.task_body
    assert spec.includes == [rules.as_posix()]
    assert spec.allowed_read_files == ["tasks/fixture/input.txt"]
    assert spec.allowed_write_files == ["tasks/fixture/output.txt"]
    assert "Fixture Style Guide" in spec.task_includes
