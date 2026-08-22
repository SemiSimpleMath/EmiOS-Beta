"""Epitaphs reach their readers WHOLE (2026-08-22, owner ruling — repeated).

A cut-off reason in a prompt is how dead chains get re-laid: "user declined ...
DO NOT RE" sliced at 100 chars reads as nothing. The why-lines in the architect's
graph render and the steward's portfolio, and the WO-level ending reason in the
steward's done/dropped logs, all render FULL; the finalizer stores its resolve
reasoning un-cut and reads a node's result un-cut.
"""
from __future__ import annotations

from app.assistant.control_nodes.strategic_planner_wo_prep_node import (
    _abandoned_line, _goal_epitaph,
)
from app.assistant.control_nodes.work_architect_node import _render_existing_graph
from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio

LONG_TAIL = "x" * 220
LONG_REASON = f"user declined the whole objective after review {LONG_TAIL} DO NOT RECREATE"


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _mk_wo(store, title="Epitaph WO"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    return wo.id, wo.goal_node_id


def test_wo_epitaph_reaches_the_dropped_log_whole():
    store = _store()
    wid, gid = _mk_wo(store)
    store.apply("set_work_status", {"work_id": wid, "status": "abandoned",
                                    "reason": LONG_REASON}, actor="steward")
    wo = store.load(wid)
    assert "DO NOT RECREATE" in _goal_epitaph(wo)
    line = _abandoned_line({"title": "Epitaph WO", "updated_at": ""}, wo)
    assert "DO NOT RECREATE" in line


def test_node_epitaph_renders_whole_in_architect_graph_and_portfolio():
    store = _store()
    wid, gid = _mk_wo(store)
    store.apply("add_node", {"work_id": wid, "id": "dead", "type": "subtask",
                             "parent_id": gid, "title": "a pruned step"})
    store.apply("set_status", {"work_id": wid, "node_id": "dead", "status": "abandoned",
                               "reason": LONG_REASON}, actor="architect")
    wo = store.load(wid)
    assert "DO NOT RECREATE" in _render_existing_graph(wo)
    assert "DO NOT RECREATE" in render_work_portfolio(wo)
