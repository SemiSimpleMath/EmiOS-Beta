"""An email item carries the pod holding its body, and that handle reaches the worker.

2026-09-13. A worker was told its answer lived in "newsletter [7667]". `short_id` is a
prompt label: no tool in the system accepts one, and it is not unique — 7667 was assigned
to both a school newsletter and an unrelated June chat about broken monitors. So the worker
had a number it could not open. It asked the user three times, watched those tickets
expire, and then spent 117 nodes and fifteen levels of recursion rebuilding from the open
web what was sitting in a 1,978-character pod the entire time.

The standing rule is pod_id canonical or invisible. Since 2026-09-30 an email IS its pod
(pod_store/email_pods.py): dayflow reads today's emails from the pods, each record carries
its own pod_id, and there is no lookup that could miss. Pinned here: the pod id is
deterministic from the Gmail account and uid (existing references keep resolving), the id
rides the ingested item, and it survives into the goal content, which is the only
description of its origin the worker ever sees.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.assistant.control_nodes.strategic_planner_wo_persist_node import (
    StrategicPlannerWoPersistNode,
)
from app.assistant.dayflow_orchestrator.input_message_builder import _build_email_message
from app.assistant.pod_store.email_pods import email_pod_id

NOW = datetime(2026, 9, 13, 16, 14, 56, tzinfo=timezone.utc)
POD = "datapod:email:0b1d896d5227d5b3c0d58fbc"
EMAIL = {
    "uid": "1a09b83febc5961f",
    "account_id": "google_user_primary",
    "subject": "The Warrior Way",
    "sender": "Woodbridge High School via ParentSquare",
    "summary": "Newsletter with Reflections deadline and Club Drive.",
    "body": "PTSA Reflections submissions are due October 9, 2026.",
    "importance": 8,
    "date_received": "",
    "pod_id": POD,
}


def test_the_pod_id_is_deterministic_from_the_gmail_message():
    """The id every existing reference already uses: the move to pods changed no email's id."""
    assert email_pod_id("google_user_primary", "1a09b83febc5961f") == POD


def test_an_email_item_carries_its_pod():
    msg = _build_email_message(email_data=EMAIL, now_utc=NOW)
    assert msg.metadata["pod_id"] == POD


# --------------------------------------------------------------------------- #
# The handle has to survive into the goal, which is all the worker sees
# --------------------------------------------------------------------------- #

def _summary_for(meta, monkeypatch):
    """Run the REAL persist node over one admitted item and capture the goal text it writes.

    Deliberately not a re-implementation of the node's string building: a test that restates
    the logic passes forever regardless of what production does.
    """
    written = {}

    class _Goal:
        id, status, content = "goal_1", "dispatched", "Do the thing."

    class _WO:
        id = "work_1"
        updated_at = NOW
        constraints = {}
        goal_node_id = "goal_1"
        nodes = {"goal_1": _Goal()}

    class _Store:
        def load(self, wid):
            return _WO()

        def apply(self, op, data, actor=None):
            written["content"] = data.get("content", "")

    class _BB:
        def __init__(self, admitted):
            self._admitted = admitted

        def get_state_value(self, key, default=None):
            return self._admitted if key == "admitted_artifacts" else default

        def update_state_value(self, *a, **k):
            pass

    monkeypatch.setattr(
        "app.assistant.dayflow_orchestrator.work_store.get_dayflow_work_store",
        lambda: _Store(), raising=False)
    monkeypatch.setattr(
        "app.assistant.dayflow_orchestrator.dayflow_item_writer.write_dayflow_item",
        lambda *a, **k: None, raising=False)

    node = StrategicPlannerWoPersistNode.__new__(StrategicPlannerWoPersistNode)
    node.name = "test"
    node.blackboard = _BB([{"metadata": meta}])
    node._close_consumed_items([{"work_id": "work_1",
                                 "based_on": [meta.get("item_id")]}])
    return written.get("content", "")


def test_the_goal_line_carries_the_pod_handle(monkeypatch):
    msg = _build_email_message(email_data=EMAIL, now_utc=NOW)
    line = _summary_for(msg.metadata, monkeypatch)
    assert POD in line
    assert "pod_fetch" in line
    assert msg.metadata["email_summary"] in line, "the informative email summary and full-source handle are kept"


def test_the_shared_macro_renders_the_handle_only_when_present():
    """The item block agents read must show the pod, and show nothing when there isn't one."""
    import io
    src = io.open(
        r"E:\EmiAi_sqlite\app\assistant\agents\shared\macros\dayflow_items.j2",
        encoding="utf-8").read()
    assert 'meta.get("pod_id")' in src
    assert "pod_fetch" in src
    # Guarded by an if, so an item without a pod renders no dead handle.
    assert '{%- if meta.get("pod_id") %}' in src
