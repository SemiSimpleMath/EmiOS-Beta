"""Shared fixtures for dayflow pipeline tests.

Sets USE_TEST_DB *before* any project imports so every DB call
hits an isolated SQLite file that is wiped between tests.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# ── Force test DB before any project import ──────────────────────
os.environ["USE_TEST_DB"] = "true"
os.environ["TEST_DB_NAME"] = "test_dayflow_pipeline"

# Prevent force_test_db from overriding our DB name to 'test_emidb'.
# We call get_session() without force_test_db — the env vars above are enough.


import pytest

# Bootstrap DI (agents, tools, managers, resources, blackboard).
import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.database.db_handler import initialize_database, UnifiedLog2026
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session


# ── Fixtures ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_db():
    """Wipe and recreate the dayflow tables before each test."""
    # Use get_session() without force_test_db — the env vars handle routing.
    # force_test_db=True would override TEST_DB_NAME to 'test_emidb'.
    session = get_session()
    engine = session.bind
    from app.assistant.database.db_handler import Base
    Base.metadata.create_all(engine)
    session.close()

    session = get_session()
    try:
        session.query(UnifiedLog2026).delete()
        session.commit()
    finally:
        session.close()
    yield
    session = get_session()
    try:
        session.query(UnifiedLog2026).delete()
        session.commit()
    finally:
        session.close()


class FakeBlackboard:
    """Minimal blackboard that reads/writes a plain dict."""

    def __init__(self, state: Optional[Dict[str, Any]] = None):
        self._state: Dict[str, Any] = dict(state or {})

    def get_state_value(self, key: str, default=None):
        return self._state.get(key, default)

    def update_state_value(self, key: str, value):
        self._state[key] = value

    def dump(self) -> Dict[str, Any]:
        """Return a snapshot of all state (useful for assertions)."""
        return dict(self._state)


@pytest.fixture
def blackboard():
    return FakeBlackboard()


# ── Seed helpers ─────────────────────────────────────────────────

def make_dayflow_message(
    *,
    item_id: Optional[str] = None,
    short_id: Optional[int] = None,
    source_type: str = "plan_task",
    event_type: str = "planned_task",
    state: str = "important_open",
    summary: str = "Test task",
    plan_id: str = "",
    importance: str = "medium",
    actionability: str = "actionable",
    created_at: Optional[datetime] = None,
    last_reviewed_at: Optional[datetime] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Message:
    """Build a dayflow Message ready for persist_dayflow_items()."""
    now_utc = datetime.now(timezone.utc)
    item_id = item_id or f"task:{uuid.uuid4().hex[:12]}"
    meta = {
        "item_id": item_id,
        "source_type": source_type,
        "event_type": event_type,
        "state": state,
        "summary": summary,
        "importance": importance,
        "actionability": actionability,
        "created_at": (created_at or now_utc).isoformat(),
        "last_reviewed_at": (last_reviewed_at or now_utc).isoformat(),
        "data_type": "dayflow_input_item",
    }
    if short_id is not None:
        meta["short_id"] = short_id
    if plan_id:
        meta["plan_id"] = plan_id
    if extra_meta:
        meta.update(extra_meta)

    return Message(
        id=item_id,
        data_type="dayflow_input_item",
        sender="test_seed",
        content=summary,
        room_id="dayflow_orchestrator",
        metadata=meta,
        timestamp=created_at or now_utc,
    )


def make_plan_synopsis_message(
    *,
    plan_id: str,
    synopsis: str = "Test plan",
    objective: str = "Test objective",
    state: str = "active",
    created_at: Optional[datetime] = None,
) -> Message:
    """Build a plan synopsis Message."""
    now_utc = datetime.now(timezone.utc)
    item_id = f"plan_synopsis:{plan_id}"
    meta = {
        "item_id": item_id,
        "source_type": "plan_synopsis",
        "event_type": "plan_overview",
        "state": state,
        "plan_id": plan_id,
        "synopsis": synopsis,
        "objective": objective,
        "success_criteria": "",
        "step_outline": [],
        "data_type": "dayflow_input_item",
        "actionability": "context_only",
        "created_at": (created_at or now_utc).isoformat(),
        "last_reviewed_at": (created_at or now_utc).isoformat(),
    }
    return Message(
        id=item_id,
        data_type="dayflow_input_item",
        sender="strategic_planner",
        content=synopsis,
        room_id="dayflow_orchestrator",
        metadata=meta,
        timestamp=created_at or now_utc,
    )


def make_chat_message(
    *,
    summary: str = "User said something",
    created_at: Optional[datetime] = None,
    short_id: Optional[int] = None,
) -> Message:
    """Build a chat dayflow item."""
    now_utc = datetime.now(timezone.utc)
    item_id = f"chat:{uuid.uuid4().hex[:8]}"
    meta = {
        "item_id": item_id,
        "source_type": "chat",
        "event_type": "user_chat",
        "state": "artifact",
        "summary": summary,
        "data_type": "dayflow_input_item",
        "created_at": (created_at or now_utc).isoformat(),
        "last_reviewed_at": (created_at or now_utc).isoformat(),
    }
    if short_id is not None:
        meta["short_id"] = short_id
    return Message(
        id=item_id,
        data_type="dayflow_input_item",
        sender="user",
        content=summary,
        room_id="dayflow_orchestrator",
        metadata=meta,
        timestamp=created_at or now_utc,
    )


def make_action_log_message(
    *,
    summary: str = "Dispatched something",
    plan_id: str = "",
    task_id: str = "",
    created_at: Optional[datetime] = None,
) -> Message:
    """Build an action_log dayflow item."""
    now_utc = datetime.now(timezone.utc)
    item_id = f"action_log:{uuid.uuid4().hex[:8]}"
    meta = {
        "item_id": item_id,
        "source_type": "action_log",
        "event_type": "dispatch",
        "state": "closed",
        "summary": summary,
        "plan_id": plan_id,
        "task_id": task_id,
        "data_type": "dayflow_input_item",
        "created_at": (created_at or now_utc).isoformat(),
        "last_reviewed_at": (created_at or now_utc).isoformat(),
    }
    return Message(
        id=item_id,
        data_type="dayflow_input_item",
        sender="action_selector",
        content=summary,
        room_id="dayflow_orchestrator",
        metadata=meta,
        timestamp=created_at or now_utc,
    )


def seed_items(items: List[Message]) -> int:
    """Persist a list of Messages to the test DB. Returns count."""
    from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch
    batch = []
    for msg in items:
        meta = dict(msg.metadata) if isinstance(msg.metadata, dict) else {}
        meta.setdefault("item_id", msg.id)
        meta.setdefault("summary", msg.content or "")
        meta.setdefault("source_type", meta.get("source_type", ""))
        batch.append(meta)
    return write_dayflow_items_batch(batch, caller="test_seed")


def load_all_items() -> List[Dict[str, Any]]:
    """Load all dayflow items from test DB (including terminal)."""
    from app.assistant.dayflow_orchestrator.state_store import load_existing_dayflow_items
    return load_existing_dayflow_items(include_terminal=True)


def load_item_by_id(item_id: str) -> Optional[Dict[str, Any]]:
    """Load a single item by item_id from test DB."""
    from app.assistant.dayflow_orchestrator.state_store import get_latest_dayflow_item_by_id
    return get_latest_dayflow_item_by_id(item_id, include_terminal=True)


def get_meta(item: Dict[str, Any]) -> Dict[str, Any]:
    """Extract metadata dict from an item."""
    from app.assistant.dayflow_orchestrator.contracts import get_meta as _get_meta
    return _get_meta(item)
