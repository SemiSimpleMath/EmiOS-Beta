"""Isolation for the system-audit register tests.

These tests wipe system_audit_case before each test. Without a test DB they ran
against the live emi.db (deleting the owner's audit cases) and read its real
work_objects table; under a full run another conftest's USE_TEST_DB routed them
to a test DB with no work_objects table, so the work-id lookup failed.

The env is set at import (an isolated run) and again per test (a full run, where
other test modules rewrite the shared env at collection time). The work-id
lookup is substituted with an empty set, as case_store._known_work_ids invites;
a test that needs known ids patches it again.
"""
from __future__ import annotations

import os
import tempfile

_TEST_DB_NAME = "test_system_audit"
_WORK_DB = os.path.join(tempfile.gettempdir(), "emi_test_system_audit_work.db")

os.environ["USE_TEST_DB"] = "true"
os.environ["TEST_DB_NAME"] = _TEST_DB_NAME
os.environ.setdefault("DAYFLOW_WORK_DB", _WORK_DB)

import pytest

import app.assistant.tests.test_setup  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_audit_db(monkeypatch):
    monkeypatch.setenv("USE_TEST_DB", "true")
    monkeypatch.setenv("TEST_DB_NAME", _TEST_DB_NAME)
    monkeypatch.delenv("TEST_DATABASE_URI_EMI", raising=False)
    from app.assistant.system_audit import case_store
    monkeypatch.setattr(case_store, "_known_work_ids", lambda: set())
    yield
