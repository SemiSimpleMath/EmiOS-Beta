"""A broken live store must never appear as an empty alternate database."""
from unittest.mock import Mock, MagicMock
import pytest
from work_objects.ui import blueprint
from app.assistant.dayflow_orchestrator import work_store


def test_ui_propagates_live_store_failure(monkeypatch):
    monkeypatch.setattr(blueprint, "_store", None)
    monkeypatch.setattr(work_store, "get_dayflow_work_store", Mock(side_effect=OSError("live store unavailable")))
    with pytest.raises(OSError, match="live store unavailable"):
        blueprint._get_store()


@pytest.mark.parametrize("migration", [work_store._migrate_node_active_to_dispatched, work_store._migrate_parked_asks_to_dispatched])
def test_failed_initialization_migration_propagates(migration):
    conn = MagicMock()
    conn.execute.side_effect = OSError("migration storage failure")
    with pytest.raises(OSError, match="migration storage failure"):
        migration(conn)
