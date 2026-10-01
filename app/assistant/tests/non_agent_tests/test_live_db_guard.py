"""The root conftest keeps every test off the live databases."""
import sqlite3

import pytest

from app.assistant.utils.path_utils import get_data_dir


def _forget(fragment):
    """The guard fails the session for every refused open; these refusals are the test's own."""
    opens = sqlite3.connect.refused
    opens[:] = [entry for entry in opens if fragment not in entry]


@pytest.mark.parametrize("target", ["emi.db", "data/emi.db", "task_work.db"])
def test_opening_a_live_database_is_refused(target):
    live = get_data_dir() / target
    with pytest.raises(RuntimeError, match="live database"):
        sqlite3.connect(str(live))
    with pytest.raises(RuntimeError, match="live database"):
        sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True)
    _forget(live.name)


def test_test_databases_and_memory_open(tmp_path):
    sqlite3.connect(":memory:").close()
    sqlite3.connect(str(tmp_path / "x.db")).close()


def test_project_defaults_point_at_test_databases():
    from app.models.base import get_database_uri
    from app.assistant.dayflow_orchestrator.work_store import dayflow_work_db_path
    assert "/emi.db" not in get_database_uri()
    assert not dayflow_work_db_path().endswith("emi.db")
