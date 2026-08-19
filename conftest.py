import pytest


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
