"""An email item carries the pod holding its body, and that handle reaches the worker.

2026-09-13. A worker was told its answer lived in "newsletter [7667]". `short_id` is a
prompt label: no tool in the system accepts one, and it is not unique — 7667 was assigned
to both a school newsletter and an unrelated June chat about broken monitors. So the worker
had a number it could not open. It asked the user three times, watched those tickets
expire, and then spent 117 nodes and fifteen levels of recursion rebuilding from the open
web what was sitting in a 1,978-character pod the entire time.

The standing rule is pod_id canonical or invisible. The prompt was showing neither.

Three seams are pinned here: the deterministic lookup from an upstream email id to its pod,
the stamp onto the ingested item, and the handle surviving into the goal content, which is
the only description of its origin the worker ever sees.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.assistant.control_nodes.strategic_planner_wo_persist_node import (
    StrategicPlannerWoPersistNode,
)
from app.assistant.dayflow_orchestrator.input_message_builder import (
    _build_email_message, _email_pod_id,
)

NOW = datetime(2026, 9, 13, 16, 14, 56, tzinfo=timezone.utc)
EMAIL = {
    "uid": "1a09b83febc5961f",
    "account_id": "google_user_primary",
    "subject": "The Warrior Way",
    "sender": "Woodbridge High School via ParentSquare",
    "summary": "Newsletter with Reflections deadline and Club Drive.",
    "body": "PTSA Reflections submissions are due October 9, 2026.",
    "importance": 8,
    "date_received": "",
}
POD = "datapod:email:0b1d896d5227d5b3c0d58fbc"


class _Pod:
    pod_id = POD


@pytest.fixture
def stub_store(monkeypatch):
    """A PodStore whose find_by_source_ref records the ref it was asked for."""
    seen = {}

    class _Store:
        def find_by_source_ref(self, ref, *, kind=None):
            seen["ref"], seen["kind"] = ref, kind
            return _Pod() if ref == "repo_email::google_user_primary::1a09b83febc5961f" else None

    monkeypatch.setattr("app.assistant.pod_store.pod_store.PodStore", _Store)
    return seen


def test_the_lookup_joins_on_the_upstream_id_not_a_phrase(stub_store):
    assert _email_pod_id(account_id="google_user_primary", uid="1a09b83febc5961f") == POD
    assert stub_store["ref"] == "repo_email::google_user_primary::1a09b83febc5961f"
    assert stub_store["kind"] == "email", "must not match a pod of some other kind"


def test_an_email_item_carries_its_pod(stub_store):
    msg = _build_email_message(email_data=EMAIL, now_utc=NOW)
    assert msg.metadata["pod_id"] == POD


def test_an_email_with_no_pod_yet_carries_an_empty_handle(stub_store):
    other = dict(EMAIL, uid="not-yet-classified")
    msg = _build_email_message(email_data=other, now_utc=NOW)
    assert msg.metadata["pod_id"] == "", "an absent handle is honest; a wrong one is the bug"


def test_a_pod_lookup_failure_never_blocks_ingestion(monkeypatch):
    class _Broken:
        def find_by_source_ref(self, ref, *, kind=None):
            raise RuntimeError("pod store down")

    monkeypatch.setattr("app.assistant.pod_store.pod_store.PodStore", _Broken)
    msg = _build_email_message(email_data=EMAIL, now_utc=NOW)
    assert msg.metadata["pod_id"] == ""
    assert msg.metadata["email_subject"] == "The Warrior Way", "the item is still worth having"


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


def test_the_goal_line_carries_the_pod_handle(stub_store, monkeypatch):
    msg = _build_email_message(email_data=EMAIL, now_utc=NOW)
    line = _summary_for(msg.metadata, monkeypatch)
    assert POD in line
    assert "pod_fetch" in line
    assert msg.metadata["summary"] in line, "the summary is kept, the handle is added"


def test_an_item_with_no_pod_gets_no_dangling_handle(stub_store, monkeypatch):
    other = dict(EMAIL, uid="not-yet-classified")
    msg = _build_email_message(email_data=other, now_utc=NOW)
    line = _summary_for(msg.metadata, monkeypatch)
    assert "full content" not in line
    assert "pod_fetch" not in line


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
