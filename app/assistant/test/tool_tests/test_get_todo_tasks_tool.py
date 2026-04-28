"""
Tests for ToDoTool.handle_get_tasks — agent-side retrieval of Google Tasks.

Design intent (from the tool + user):
- The UI reads tasks from EventRepository, not from tool ``content``. So the
  agent-facing summary can be terse (count-only) without breaking the UX.
- The important correctness properties are:
  * ``data_list`` carries full task rows for downstream consumers
  * filters (task_name, date range, include_completed) work correctly
  * ``repo_update`` flag gates repository writes
  * ``data_type`` is stamped on every row

These tests lock those behaviors in.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _task(*, task_id, title, status="needsAction", due=None, tasklist="Inbox"):
    """Build a Google-Tasks-shaped task dict."""
    row = {
        "id": task_id,
        "title": title,
        "status": status,
        "tasklist_title": tasklist,
    }
    if due is not None:
        row["due"] = due
    return row


class _NoopRepoManager:
    def __init__(self):
        self.stored = []  # list of (task_id, event_data, data_type)
        self.synced = []  # list of (server_ids, data_type)

    def store_event(self, task_id, *, event_data, data_type):
        self.stored.append((task_id, event_data, data_type))

    def sync_events_with_server(self, server_ids, data_type):
        self.synced.append((list(server_ids), data_type))


def _build_tool(monkeypatch, canned_tasks):
    """Build a ToDoTool with its Google-API call stubbed.

    We bypass __init__ (which would try to authenticate to Google) and
    wire in the minimal shape the handler needs.
    """
    from app.assistant.lib.core_tools.todo_tool.todo_tool import ToDoTool
    from app.assistant.lib.core_tools.todo_tool import todo_tool as td_mod

    tool = ToDoTool.__new__(ToDoTool)
    tool.name = "todo_tool"
    repo = _NoopRepoManager()
    tool.repo_manager = repo

    def _stub_retrieve(*_a, **_kw):
        # Return a fresh copy so mutations inside the handler don't leak
        # into other tests.
        return [dict(t) for t in canned_tasks]

    monkeypatch.setattr(td_mod, "retrieve_all_tasks_across_lists", _stub_retrieve, raising=True)
    return tool, repo


def _get_tasks(tool, **arguments):
    return tool.handle_get_tasks(arguments)


# ----------------------------------------------------------------------
# Core behavior
# ----------------------------------------------------------------------

def test_returns_non_completed_tasks_by_default(monkeypatch):
    """include_completed defaults to False → completed tasks must be filtered out."""
    tool, _repo = _build_tool(monkeypatch, [
        _task(task_id="a", title="Buy groceries"),
        _task(task_id="b", title="Old todo that was finished", status="completed"),
        _task(task_id="c", title="Send Katy the link"),
    ])

    result = _get_tasks(tool)
    titles = [r["title"] for r in result.data_list]
    assert titles == ["Buy groceries", "Send Katy the link"]
    assert all(r.get("status") != "completed" for r in result.data_list)


def test_include_completed_true_keeps_completed_tasks(monkeypatch):
    tool, _repo = _build_tool(monkeypatch, [
        _task(task_id="a", title="Buy groceries"),
        _task(task_id="b", title="Done task", status="completed"),
    ])

    result = _get_tasks(tool, include_completed=True)
    titles = [r["title"] for r in result.data_list]
    assert "Buy groceries" in titles
    assert "Done task" in titles


def test_task_name_substring_filter(monkeypatch):
    """task_name filter is case-insensitive substring match against title."""
    tool, _repo = _build_tool(monkeypatch, [
        _task(task_id="a", title="Call Sam's school"),
        _task(task_id="b", title="Buy milk"),
        _task(task_id="c", title="Research Sam's internship deadlines"),
    ])

    result = _get_tasks(tool, task_name="peter")
    titles = [r["title"] for r in result.data_list]
    assert len(titles) == 2
    assert "Call Sam's school" in titles
    assert "Research Sam's internship deadlines" in titles
    assert "Buy milk" not in titles


def test_date_range_filter_keeps_tasks_due_in_window(monkeypatch):
    """start_date / end_date restrict to tasks whose ``due`` is in the window.
    Tasks with no due date pass through (informational — not time-bound)."""
    tool, _repo = _build_tool(monkeypatch, [
        _task(task_id="a", title="Before window", due="2026-04-10T00:00:00.000Z"),
        _task(task_id="b", title="Inside window", due="2026-04-17T00:00:00.000Z"),
        _task(task_id="c", title="After window", due="2026-05-01T00:00:00.000Z"),
        _task(task_id="d", title="No due date"),
    ])

    result = _get_tasks(
        tool,
        start_date="2026-04-15T00:00:00Z",
        end_date="2026-04-20T23:59:59Z",
    )
    titles = [r["title"] for r in result.data_list]
    assert "Inside window" in titles
    assert "No due date" in titles  # no-due passes through
    assert "Before window" not in titles
    assert "After window" not in titles


def test_data_list_rows_carry_full_task_fields(monkeypatch):
    """Every row in data_list should preserve the Google-Tasks fields so
    downstream consumers (UI, repo) can render them fully.
    """
    tool, _repo = _build_tool(monkeypatch, [
        _task(
            task_id="task_1",
            title="File 2025 taxes",
            due="2026-04-15T00:00:00.000Z",
            tasklist="Finance",
        ),
    ])

    result = _get_tasks(tool)
    assert len(result.data_list) == 1
    row = result.data_list[0]
    assert row["id"] == "task_1"
    assert row["title"] == "File 2025 taxes"
    assert row["due"] == "2026-04-15T00:00:00.000Z"
    assert row["tasklist_title"] == "Finance"
    assert row["status"] == "needsAction"


def test_data_type_todo_task_stamped_on_every_row(monkeypatch):
    """Every returned task must have data_type='todo_task' so downstream
    consumers can identify the source without inspecting the parent tool.
    """
    tool, _repo = _build_tool(monkeypatch, [
        _task(task_id="a", title="Task A"),
        _task(task_id="b", title="Task B"),
        _task(task_id="c", title="Task C"),
    ])

    result = _get_tasks(tool)
    for row in result.data_list:
        assert row.get("data_type") == "todo_task"


def test_repo_update_false_does_not_write(monkeypatch):
    """Default agent read path: repo_update=False → no writes to the repository."""
    tool, repo = _build_tool(monkeypatch, [
        _task(task_id="a", title="Task A"),
        _task(task_id="b", title="Task B"),
    ])

    _get_tasks(tool)  # default repo_update=False
    assert repo.stored == []
    assert repo.synced == []


def test_repo_update_true_writes_every_returned_task(monkeypatch):
    """Maintenance path: repo_update=True → every task row is written to the repo
    AND a single sync_events_with_server call carries all server ids.
    """
    tool, repo = _build_tool(monkeypatch, [
        _task(task_id="a", title="Task A"),
        _task(task_id="b", title="Task B"),
        _task(task_id="c", title="Task C", status="completed"),
    ])

    _get_tasks(tool, repo_update=True, include_completed=True)
    # All three tasks should have been stored
    stored_ids = [item[0] for item in repo.stored]
    assert stored_ids == ["a", "b", "c"]
    # All store_event calls should have data_type="todo_task"
    for _id, payload, data_type in repo.stored:
        assert data_type == "todo_task"
        assert payload["data_type"] == "todo_task"
    # Exactly one sync call with all three ids
    assert len(repo.synced) == 1
    synced_ids, synced_type = repo.synced[0]
    assert synced_ids == ["a", "b", "c"]
    assert synced_type == "todo_task"


def test_repo_update_skips_tasks_missing_id(monkeypatch):
    """Defensive: malformed task rows without an id should not crash the
    repo-update path; they get skipped.
    """
    tool, repo = _build_tool(monkeypatch, [
        _task(task_id="a", title="Task A"),
        {"title": "No ID — should be skipped", "status": "needsAction",
         "tasklist_title": "Inbox"},  # no 'id' or 'task_id'
        _task(task_id="c", title="Task C"),
    ])

    _get_tasks(tool, repo_update=True)
    stored_ids = [item[0] for item in repo.stored]
    assert stored_ids == ["a", "c"]


def test_empty_result_returns_empty_data_list_not_none(monkeypatch):
    """No-task edge case: data_list should be an empty list, never None.
    Callers do ``for t in result.data_list`` and a None here would crash.
    """
    tool, _repo = _build_tool(monkeypatch, [])
    result = _get_tasks(tool)
    assert result.data_list == []
    assert isinstance(result.data_list, list)


def test_filter_composition_name_plus_date_range(monkeypatch):
    """Filters compose correctly: name and date range both applied."""
    tool, _repo = _build_tool(monkeypatch, [
        _task(task_id="a", title="Call Sam about soccer", due="2026-04-17T00:00:00.000Z"),
        _task(task_id="b", title="Buy milk", due="2026-04-17T00:00:00.000Z"),
        _task(task_id="c", title="Call Sam about homework", due="2026-05-01T00:00:00.000Z"),
    ])

    result = _get_tasks(
        tool,
        task_name="peter",
        start_date="2026-04-15T00:00:00Z",
        end_date="2026-04-20T23:59:59Z",
    )
    titles = [r["title"] for r in result.data_list]
    assert titles == ["Call Sam about soccer"]
