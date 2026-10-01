"""manager_tests/ holds integration scripts run directly against the real KG and
LLMs (CLAUDE.md, "Running Tests"); some mutate the live KG. Pytest must not
collect them: a `*_test.py` name and `test_*` functions here are script names,
not pytest tests.
"""
collect_ignore_glob = ["*"]
