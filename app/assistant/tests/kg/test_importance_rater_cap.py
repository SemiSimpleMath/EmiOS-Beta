"""kg_importance_rater per-run cap (watchdog-risk fix, 2026-06-13).

regenerate_edge_importance had no total cap — one run rated EVERY unrated
edge (batched), so a bulk re-extraction could turn the 30-min routine into a
graph-wide LLM grind with no max_run_seconds to alert. The fix adds max_edges
(a .limit() on the only_unrated query, mirroring backfill_untagged_nodes).
Because only_unrated is a SHRINKING selector, the remainder converges over
runs. This test proves both: the cap bounds one run, and repeated runs drain
the backlog to zero.

Uses the kg conftest (isolated test DB). The rater agent is mocked, so no LLM.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.importance import scoring
from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.models.base import get_session


def _mk_node(label):
    nid = str(uuid.uuid4())
    s = get_session()
    try:
        s.add(Node(id=nid, label=label, node_type="Entity"))
        s.commit()
    finally:
        s.close()
    return nid


def _mk_edge(src, tgt, rel):
    eid = str(uuid.uuid4())
    s = get_session()
    try:
        s.add(Edge(id=eid, source_id=src, target_id=tgt, relationship_type=rel))
        s.commit()
    finally:
        s.close()
    return eid


def _rated_count(eids):
    s = get_session()
    try:
        return (
            s.query(Edge)
            .filter(Edge.id.in_(eids), Edge.importance.isnot(None))
            .count()
        )
    finally:
        s.close()


def test_edge_importance_cap_bounds_run_and_converges(monkeypatch):
    src, tgt = _mk_node("Src"), _mk_node("Tgt")
    eids = [_mk_edge(src, tgt, f"rel_{i}") for i in range(5)]

    # Rater returns no explicit ratings → each batched edge still gets the
    # default score written, so it becomes rated (drops out of only_unrated).
    fake_agent = SimpleNamespace(action_handler=lambda msg: SimpleNamespace(data={}))
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: fake_agent)

    def _run():
        return scoring.regenerate_edge_importance(
            batch_size=50, only_unrated=True, max_edges=2, scope_context=None)

    _run()
    assert _rated_count(eids) == 2          # cap bounds this run
    _run()
    assert _rated_count(eids) == 4          # converges...
    _run()
    assert _rated_count(eids) == 5          # ...to zero remaining
    # Idempotent once drained: nothing left unrated, so a further run is a no-op.
    assert _run() == 0


def test_edge_importance_uncapped_by_default(monkeypatch):
    src, tgt = _mk_node("Src2"), _mk_node("Tgt2")
    eids = [_mk_edge(src, tgt, f"r_{i}") for i in range(4)]
    fake_agent = SimpleNamespace(action_handler=lambda msg: SimpleNamespace(data={}))
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: fake_agent)

    scoring.regenerate_edge_importance(
        batch_size=50, only_unrated=True, scope_context=None)  # no max_edges
    assert _rated_count(eids) == 4          # all rated in one run (unchanged default)
