import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

from app.assistant.utils.path_utils import get_data_dir


# ── Tests never touch a live database (2026-09-30) ─────────────────────────────────────────────
# Database choice used to be opt-in per test module: get_database_uri() returns the live emi.db
# unless USE_TEST_DB=true, and the dayflow work store returns emi.db unless DAYFLOW_WORK_DB is set.
# A test file run on its own that did not set them ran its fixtures against the owner's database
# (2026-09-30: an isolated system_audit run emptied system_audit_case and wrote work objects and a
# chat message into emi.db). This root conftest loads before any test module, for a full run or a
# single file, so:
#   1. test-database defaults are set before anything imports the project;
#   2. opening a live database file from this process raises, and the session fails even if a
#      test swallowed the error. Reads are refused too: a test may not depend on the owner's data.
# Live = a *.db directly in the data dir or data/ whose name is not a test database. Standalone
# manager scripts (`python <script>`) do not load this file and still reach the real KG on purpose.
os.environ["USE_TEST_DB"] = "true"
os.environ.setdefault("TEST_DB_NAME", "test_emidb")
os.environ.setdefault("DAYFLOW_WORK_DB", os.path.join(tempfile.gettempdir(), "emi_test_work.db"))

_LIVE_DIRS = {get_data_dir().resolve(), (get_data_dir() / "data").resolve()}
_TEST_PREFIXES = ("test_", "e2e_sandbox_")
_live_db_opens: list = []


def _db_file(database, uri: bool):
    text = os.fspath(database) if not isinstance(database, str) else database
    if text in ("", ":memory:"):
        return None
    if uri or text.startswith("file:"):
        parsed = urlparse(text)
        text = unquote(parsed.path or parsed.netloc)
        if not text or text == ":memory:" or "mode=memory" in parsed.query:
            return None
        if len(text) > 2 and text[0] == "/" and text[2] == ":":   # file:///E:/x.db -> E:/x.db
            text = text[1:]
    return Path(text).resolve()


def _is_live(path) -> bool:
    return (path is not None and path.suffix == ".db" and path.parent in _LIVE_DIRS
            and not path.name.startswith(_TEST_PREFIXES))


def _guard(connect):
    def guarded(database, *args, **kwargs):
        path = _db_file(database, bool(kwargs.get("uri")))
        if _is_live(path):
            where = os.environ.get("PYTEST_CURRENT_TEST", "(collection)")
            _live_db_opens.append(f"{where}: {path}")
            raise RuntimeError(f"A test tried to open the live database {path} ({where}). Tests use "
                               f"test databases only; see the guard in the root conftest.py.")
        return connect(database, *args, **kwargs)
    guarded.__wrapped__ = connect
    guarded.refused = _live_db_opens   # test_live_db_guard reads it
    return guarded


# sqlite3.connect is re-exported from sqlite3.dbapi2; SQLAlchemy's pysqlite dialect calls the latter.
sqlite3.dbapi2.connect = _guard(sqlite3.dbapi2.connect)
sqlite3.connect = sqlite3.dbapi2.connect


def pytest_sessionfinish(session, exitstatus):
    if _live_db_opens:
        print("\n\nLIVE DATABASE OPENED BY TESTS (refused):\n  " + "\n  ".join(_live_db_opens))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


# Manual, human-run scripts that happen to match pytest's *_test.py pattern.
# They define no tests at all — each is a `python <path>` entry point with a
# main() — but they DO bootstrap DI and mutate sys.path at import time, so
# letting pytest import them costs real side effects to collect nothing.
#
# Two of them additionally broke collection outright: run_test.py and
# web_manager_test.py each exist in two directories with no __init__.py, so
# pytest derived the same module name twice and aborted the whole run with
# "import file mismatch". Adding __init__.py would have silenced that by making
# the imports succeed — i.e. by running the DI bootstrap during collection —
# which is the wrong trade. Not collecting them is both the fix and the truth:
# they are not tests.
#
# (The alternative is renaming them out of the pytest pattern, e.g.
# run_test.py -> run_manual.py. That is arguably cleaner but changes paths the
# maintainer invokes by hand, so it is left as the maintainer's call.)
collect_ignore = [
    "app/assistant/tests/agent_tests/dayflow_routine/run_test.py",
    "app/assistant/tests/agent_tests/health_status_writer/run_test.py",
    "app/assistant/tests/manager_tests/web/web_manager_test.py",
    "app/assistant/tests/manager_tests/web_manager/web_manager_test.py",
]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: marks tests that require real network access (deselect with -m 'not integration')",
    )
