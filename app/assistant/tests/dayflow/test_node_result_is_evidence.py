"""Invariant: a node's directive (`content`) is its IMMUTABLE identity; the manager's result is recorded as
EVIDENCE rows under the node, never by overwriting `content`. run_node mints the result as an evidence child;
node_result + the portfolio render read it from there.
"""
import os

import pytest

from work_objects.store import WorkStore
from work_objects.model import new_id
from app.assistant.dayflow_orchestrator.work_portfolio import node_result, render_work_portfolio


@pytest.fixture()
def wo_with_result(tmp_path):
    s = WorkStore(os.path.join(str(tmp_path), "wo.db"))
    wo = s.apply("create_work_object", {"title": "G", "goal_content": "G"})
    wid, gid = wo.id, wo.goal_node_id
    nid = new_id("node")
    s.apply("add_node", {"work_id": wid, "id": nid, "type": "subtask", "parent_id": gid,
                         "title": "Do X", "content": "DIRECTIVE: open Canvas and do X"}, work_id=wid)
    for st in ["actionable", "dispatched", "done"]:
        s.apply("set_status", {"work_id": wid, "node_id": nid, "status": st}, work_id=wid)
    # how run_node records the result: an evidence child, NOT a content overwrite
    s.apply("add_node", {"work_id": wid, "id": new_id("result"), "type": "evidence", "parent_id": nid,
                         "status": "assumed", "content": "RESULT: X done — found 3 items"}, work_id=wid)
    return s.load(wid), nid


def test_directive_is_immutable(wo_with_result):
    wo, nid = wo_with_result
    assert wo.nodes[nid].content == "DIRECTIVE: open Canvas and do X"


def test_node_result_reads_the_evidence(wo_with_result):
    wo, nid = wo_with_result
    assert node_result(wo, wo.nodes[nid]) == "RESULT: X done — found 3 items"


def test_render_shows_evidence_result_not_directive(wo_with_result):
    wo, _ = wo_with_result
    r = render_work_portfolio(wo)
    assert "RESULT: X done — found 3 items" in r
    assert "RESULT: DIRECTIVE" not in r


def test_node_without_evidence_has_no_result(tmp_path):
    s = WorkStore(os.path.join(str(tmp_path), "wo.db"))
    wo = s.apply("create_work_object", {"title": "G", "goal_content": "G"})
    nid = new_id("node")
    s.apply("add_node", {"work_id": wo.id, "id": nid, "type": "subtask", "parent_id": wo.goal_node_id,
                         "title": "Do Y", "content": "DIRECTIVE: do Y"}, work_id=wo.id)
    wo = s.load(wo.id)
    assert node_result(wo, wo.nodes[nid]) == ""   # no evidence -> no result (NOT the directive)
