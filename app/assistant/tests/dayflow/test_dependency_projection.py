"""Dependency results must reach the consumers that build on them (2026-08-20).

The worker projection's DEPENDENCIES section inlined outputs ONLY via `produces`
edges — but the live writers (WorkPlanner findings, discharge manager-results)
land evidence as parent-linked children and never mint produces edges. Every
worker saw its dependencies as bare titles over an empty list ("already solved
— do NOT peek"), improvised (ask_kg for PR findings), and the architect churned
replans against the same blind spot (PR #16 WO, v2..v6).

Also guarded here: the full-report pod-id walk that puts the /research/<pod_id>
link on delivery tickets, the portfolio's STILL UNRUN line (the steward completed
a WO whose user hand-off had never run), and the un-truncated dependency fold in
discharge (the [:400] slice is gone by owner ruling).
"""
from __future__ import annotations

import app.assistant.dayflow_orchestrator.node_dispatch as nd
from app.assistant.control_nodes.workobject_render_node import render_work_projection
from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio
from work_objects.discharge import _render_dependencies


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _mk_wo(store, title="Projection WO"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    return wo.id, wo.goal_node_id


def _add(store, wid, node_id, *, parent, type="subtask", title="", content="",
         status=None, pod_ref=None):
    data = {"work_id": wid, "id": node_id, "type": type, "parent_id": parent,
            "title": title or node_id, "content": content}
    if status:
        data["status"] = status
    if pod_ref:
        data["pod_ref"] = pod_ref
    store.apply("add_node", data)


def _edge(store, wid, src, dst, relation="depends_on"):
    store.apply("add_edge", {"work_id": wid, "src": src, "dst": dst, "relation": relation})


class TestDependencyProjection:

    def test_parent_linked_evidence_is_inlined(self):
        """The regression: findings/manager-results are parent-linked children."""
        store = _store()
        wid, gid = _mk_wo(store)
        _add(store, wid, "inspect", parent=gid, title="Inspect the source",
             content="Go inspect the source repository.")
        _add(store, wid, "ev1", parent="inspect", type="evidence", status="assumed",
             content="FINDING-ALPHA: the backup dir derives from the db path.")
        _add(store, wid, "assess", parent=gid, title="Assess merge safety",
             content="Assess using the inspection findings.")
        _edge(store, wid, "inspect", "assess")

        view = render_work_projection(store.load(wid), "assess")
        assert "FINDING-ALPHA" in view
        assert "do NOT peek" not in view

    def test_produces_linked_evidence_still_inlined(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _add(store, wid, "gather", parent=gid, title="Gather data")
        _add(store, wid, "ev_p", parent=gid, type="evidence", status="assumed",
             content="PRODUCED-BETA result text.")
        _edge(store, wid, "gather", "ev_p", relation="produces")
        _add(store, wid, "use", parent=gid, title="Use the data")
        _edge(store, wid, "gather", "use")

        view = render_work_projection(store.load(wid), "use")
        assert "PRODUCED-BETA" in view

    def test_bare_dependency_shows_its_own_record_or_says_so(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _add(store, wid, "step_with_record", parent=gid, title="Closed checklist step",
             content="Completed via inspection: RECORD-GAMMA.")
        _add(store, wid, "step_empty", parent=gid, title="Silent step", content="")
        _add(store, wid, "consumer", parent=gid, title="Consumer")
        _edge(store, wid, "step_with_record", "consumer")
        _edge(store, wid, "step_empty", "consumer")

        view = render_work_projection(store.load(wid), "consumer")
        assert "RECORD-GAMMA" in view
        assert "no recorded result" in view

    def test_discharge_dependency_fold_is_untruncated(self):
        long_finding = "LONGTOKEN " + ("x" * 800) + " ENDTOKEN"
        store = _store()
        wid, gid = _mk_wo(store)
        _add(store, wid, "up", parent=gid, title="Upstream")
        _add(store, wid, "ev_long", parent="up", type="evidence", status="assumed",
             content=long_finding)
        _add(store, wid, "down", parent=gid, title="Downstream")
        _edge(store, wid, "up", "down")

        info = _render_dependencies(store.load(wid), "down")
        assert "ENDTOKEN" in info          # the [:400] slice would have cut this


class TestResearchPodIds:

    def test_collects_research_pods_across_the_delivery_neighborhood(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _add(store, wid, "research", parent=gid, title="Do the research",
             pod_ref="datapod:research_finding:aaa111aaa111")
        _add(store, wid, "ev_pod", parent="research", type="evidence", status="assumed",
             content="finding with pod", pod_ref="datapod:research_finding:bbb222bbb222")
        _add(store, wid, "ev_img", parent="research", type="evidence", status="assumed",
             content="screenshot", pod_ref="datapod:image:ccc333ccc333")
        _add(store, wid, "deliver", parent=gid, title="Give the user the report")
        _edge(store, wid, "research", "deliver")

        ids = nd._research_pod_ids(store.load(wid), "deliver")
        assert set(ids) == {"datapod:research_finding:aaa111aaa111",
                            "datapod:research_finding:bbb222bbb222"}
        assert len(ids) == len(set(ids))

    def test_no_research_pods_means_no_links(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _add(store, wid, "deliver", parent=gid, title="Give the user the report")
        assert nd._research_pod_ids(store.load(wid), "deliver") == []


class TestPortfolioUnrunLine:

    def test_unrun_handoff_is_loud(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _add(store, wid, "assess", parent=gid, title="Assess the thing")
        store.apply("set_status", {"work_id": wid, "node_id": "assess", "status": "dispatched"})
        store.apply("set_status", {"work_id": wid, "node_id": "assess", "status": "done"})
        _add(store, wid, "handoff", parent=gid, title="Give the user the assessment")
        store.apply("set_status", {"work_id": wid, "node_id": "handoff", "status": "proposed"})

        view = render_work_portfolio(store.load(wid))
        assert "STILL UNRUN" in view
        assert "Give the user the assessment" in view

    def test_fully_run_wo_has_no_unrun_line(self):
        store = _store()
        wid, gid = _mk_wo(store)
        _add(store, wid, "only", parent=gid, title="The single step")
        store.apply("set_status", {"work_id": wid, "node_id": "only", "status": "dispatched"})
        store.apply("set_status", {"work_id": wid, "node_id": "only", "status": "done"})

        view = render_work_portfolio(store.load(wid))
        assert "STILL UNRUN" not in view
