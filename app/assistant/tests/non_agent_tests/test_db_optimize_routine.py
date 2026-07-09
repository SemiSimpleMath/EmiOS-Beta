"""Nightly PRAGMA optimize routine (persistence audit P2, 2026-07-09).

ANALYZE had never run on emi.db (no sqlite_stat1 across 103 tables / 351
indexes). A one-time bounded ANALYZE seeded the statistics; this routine
keeps them fresh nightly via SQLite's documented long-running-app
pattern (analysis_limit + PRAGMA optimize).
"""
from __future__ import annotations

import os

os.environ["USE_TEST_DB"] = "true"
os.environ.setdefault("TEST_DB_NAME", "test_db_optimize")

import json

import app.assistant.tests.test_setup  # noqa: F401


def test_handler_is_discovered():
    from app.assistant.routine_handlers import discover_handlers

    assert "db_optimize" in discover_handlers()


def test_handler_runs_clean_through_the_writer_queue():
    from app.assistant.database.db_handler import initialize_database
    from app.assistant.routine_handlers.db_optimize import db_optimize

    initialize_database()
    result = db_optimize()
    assert result == {"status": "ok"}


def test_routine_config_points_at_the_handler():
    cfg = json.load(open("configs/routines/public/db_optimize.json", encoding="utf-8"))
    assert cfg["id"] == "db_optimize"
    assert cfg["runner"] == "function"
    assert cfg["spec"]["function_name"] == "db_optimize"
    assert cfg["run_policy"]["type"] == "daily"
    assert cfg.get("max_run_seconds", 0) > 0
